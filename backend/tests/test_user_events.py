"""Unit tests for `UserEventBroadcaster` (issue #16, acceptance criterion A4).

Mirrors `tests/test_pointer_broadcaster.py`'s isolation style: drives the broadcaster
directly against a fake Redis pub/sub double (the PSUBSCRIBE surface), no live
datastore, no app/HTTP involved -- `radio/user_events.py`'s module docstring covers
the design rationale.

STATUS (issue #16, phase 1/RED-tests): `register`/`unregister`/`start`/`stop` are
fully implemented in `radio/user_events.py` and pinned GREEN below. Delivery through
`_relay_loop` is issue #16 phase 3 -- the delivery test at the bottom of this file is
the RED surface (its queue never receives the message, so the read times out and the
assertion fails).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from songforge.radio.user_events import _QUEUE_MAXSIZE, UserEventBroadcaster, user_channel

PREFIX = "user:"


class _FakePubSub:
    """Mirrors the `psubscribe`/`get_message`/`close` surface the relay loop uses --
    the PSUBSCRIBE analogue of `test_pointer_broadcaster.py`'s `_FakePubSub`."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.patterns: set[str] = set()
        self.closed = False

    async def psubscribe(self, pattern: str) -> None:
        self.patterns.add(pattern)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict[str, Any] | None:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True

    def feed(self, channel: str, data: str, *, msg_type: str = "pmessage") -> None:
        self._queue.put_nowait({"type": msg_type, "channel": channel, "data": data})


class _FakeRedis:
    """Hands out one shared `_FakePubSub` and counts `pubsub()` calls (idempotency) --
    mirrors `test_pointer_broadcaster.py`'s `_FakeRedis`."""

    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub()
        self.pubsub_calls = 0

    def pubsub(self) -> _FakePubSub:
        self.pubsub_calls += 1
        return self.pubsub_obj


def _make(redis: _FakeRedis) -> UserEventBroadcaster:
    return UserEventBroadcaster(redis=redis, channel_prefix=PREFIX)  # type: ignore[arg-type]


async def _wait_until(cond: Callable[[], bool], *, timeout: float = 0.5) -> bool:
    """Poll `cond` cooperatively until true or `timeout` -- lets the relay task run
    (mirrors `test_pointer_broadcaster.py::_wait_until`, kept short here since the
    RED-phase delivery test below is EXPECTED to time out)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


# ── register/unregister/start/stop lifecycle (fully implemented, GREEN) ──────────


async def test_register_tracks_the_queue_under_its_user_id() -> None:
    broadcaster = _make(_FakeRedis())

    queue = broadcaster.register("user-a")

    assert queue in broadcaster._queues["user-a"]


async def test_register_supports_multiple_clients_for_the_same_user() -> None:
    broadcaster = _make(_FakeRedis())

    q1 = broadcaster.register("user-a")
    q2 = broadcaster.register("user-a")

    assert broadcaster._queues["user-a"] == {q1, q2}


async def test_unregister_removes_the_queue_and_drops_the_user_entry_once_empty() -> None:
    broadcaster = _make(_FakeRedis())
    queue = broadcaster.register("user-a")

    broadcaster.unregister("user-a", queue)

    assert "user-a" not in broadcaster._queues


async def test_unregister_an_already_removed_queue_is_a_noop() -> None:
    broadcaster = _make(_FakeRedis())
    queue = broadcaster.register("user-a")
    broadcaster.unregister("user-a", queue)

    broadcaster.unregister("user-a", queue)  # must not raise


async def test_unregister_an_unknown_user_is_a_noop() -> None:
    broadcaster = _make(_FakeRedis())

    broadcaster.unregister("never-registered", broadcaster.register("user-a"))  # must not raise


async def test_start_psubscribes_to_the_wildcard_pattern_and_is_idempotent() -> None:
    redis = _FakeRedis()
    broadcaster = _make(redis)

    await broadcaster.start()
    await broadcaster.start()  # second call is a no-op
    try:
        assert redis.pubsub_calls == 1
        assert redis.pubsub_obj.patterns == {"user:*:events"}
    finally:
        await broadcaster.stop()


async def test_stop_cancels_the_relay_task_and_closes_the_subscription() -> None:
    redis = _FakeRedis()
    broadcaster = _make(redis)
    await broadcaster.start()

    assert broadcaster._task is not None
    await broadcaster.stop()

    assert broadcaster._task is None
    assert redis.pubsub_obj.closed is True
    await broadcaster.stop()  # idempotent -- must not raise


# ── channel helpers (fully implemented, GREEN) ────────────────────────────────────


def test_user_channel_embeds_the_prefix_and_user_id() -> None:
    assert user_channel(PREFIX, "user-a") == "user:user-a:events"


def test_user_channel_produces_distinct_channels_per_user() -> None:
    assert user_channel(PREFIX, "user-a") != user_channel(PREFIX, "user-b")


# ── Delivery (issue #16 phase 3, currently RED: `_relay_loop` is a stub) ──────────


async def test_a_published_job_failed_message_is_delivered_only_to_its_own_user() -> None:
    """Acceptance A4: a `job-failed` message published for user X is delivered to a
    client registered under X and NOT to a client registered under Y. Currently RED
    -- `UserEventBroadcaster._relay_loop` never consumes a pub/sub message yet (issue
    #16 phase 3), so `queue_x` stays empty and this assertion fails rather than
    proving delivery."""
    redis = _FakeRedis()
    broadcaster = _make(redis)
    queue_x = broadcaster.register("user-x")
    queue_y = broadcaster.register("user-y")
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(
            user_channel(PREFIX, "user-x"),
            '{"event": "job-failed", "job_id": "job-1"}',
        )

        delivered = None
        if await _wait_until(lambda: not queue_x.empty()):
            delivered = queue_x.get_nowait()

        assert delivered is not None, "job-failed was never delivered to user-x"
        assert delivered.get("job_id") == "job-1"
        assert queue_y.empty()  # never delivered to the wrong user
    finally:
        await broadcaster.stop()


async def test_two_clients_for_the_same_user_both_receive_the_message() -> None:
    """A4's "notifies them over their per-user SSE channel" must reach every tab/
    connection a user has open, not just one -- `register` supports more than one
    queue per `user_id` for exactly this reason."""
    redis = _FakeRedis()
    broadcaster = _make(redis)
    q1 = broadcaster.register("user-a")
    q2 = broadcaster.register("user-a")
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(
            user_channel(PREFIX, "user-a"), '{"event": "job-failed", "job_id": "job-1"}'
        )

        assert await _wait_until(lambda: not q1.empty())
        assert await _wait_until(lambda: not q2.empty())
        assert q1.get_nowait().get("job_id") == "job-1"
        assert q2.get_nowait().get("job_id") == "job-1"
    finally:
        await broadcaster.stop()


async def test_a_full_queue_drops_the_frame_without_raising_and_without_affecting_others() -> None:
    """A stalled/slow client for user A must never raise out of the relay loop (which
    would take down delivery for every other connected client), and must never block
    delivery to a HEALTHY second connection for the SAME user -- mirrors
    `PointerBroadcaster`'s "a stalled client drops frames" posture."""
    redis = _FakeRedis()
    broadcaster = _make(redis)
    full_queue = broadcaster.register("user-a")
    for i in range(_QUEUE_MAXSIZE):
        full_queue.put_nowait({"filler": i})
    healthy_queue = broadcaster.register("user-a")
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(
            user_channel(PREFIX, "user-a"), '{"event": "job-failed", "job_id": "job-2"}'
        )

        assert await _wait_until(lambda: not healthy_queue.empty())
        assert healthy_queue.get_nowait().get("job_id") == "job-2"
        assert full_queue.qsize() == _QUEUE_MAXSIZE  # the frame was dropped, not queued
    finally:
        await broadcaster.stop()


async def test_a_message_for_an_unregistered_user_is_a_noop() -> None:
    """No client is registered under the message's user id at all -- the relay loop
    must not raise (`self._queues.get(user_id, ())` -- an empty default, not a
    `KeyError`)."""
    redis = _FakeRedis()
    broadcaster = _make(redis)
    registered_elsewhere = broadcaster.register("user-b")
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(
            user_channel(PREFIX, "nobody-here"), '{"event": "job-failed", "job_id": "job-3"}'
        )
        # Nothing to await a delivery on; give the relay loop a chance to process the
        # message and prove (by the broadcaster still being alive afterwards) that it
        # didn't raise.
        await asyncio.sleep(0.05)
        assert registered_elsewhere.empty()  # never mis-delivered to an unrelated user
    finally:
        await broadcaster.stop()
