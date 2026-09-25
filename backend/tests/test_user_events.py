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

from songforge.radio.user_events import UserEventBroadcaster, user_channel

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
