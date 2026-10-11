"""Unit tests for `QueueDepthBroadcaster` + its `_Coalescer` (depth-in-SSE follow-up).

Mirrors `test_pointer_broadcaster.py`'s fake-pubsub approach for the fan-out paths, and
drives the coalescing policy directly through a fake clock so the "<=1 emit per window"
rule is pinned WITHOUT real time. One end-to-end coalescing test (small real window)
proves latest-wins drops the middle update.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

from songforge.radio.queue_depth import (
    QueueDepthBroadcaster,
    _Coalescer,
    depth_message,
    parse_depth_message,
    redis_queue_depth_publisher,
)

CHANNEL = "radio:queue:depth"


class _FakePubSub:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.channels: set[str] = set()
        self.closed = False
        self.raise_times = 0

    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict[str, Any] | None:
        if self.raise_times > 0:
            self.raise_times -= 1
            raise ConnectionError("redis down")
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True

    def feed(self, data: str, *, msg_type: str = "message") -> None:
        self._queue.put_nowait({"type": msg_type, "channel": CHANNEL, "data": data})


class _FakeRedis:
    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub()
        self.pubsub_calls = 0
        self.published: list[tuple[str, str]] = []

    def pubsub(self) -> _FakePubSub:
        self.pubsub_calls += 1
        return self.pubsub_obj

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


def _make(redis: _FakeRedis, *, coalesce_seconds: float = 0.0) -> QueueDepthBroadcaster:
    return QueueDepthBroadcaster(
        redis=cast(Any, redis),
        channel=CHANNEL,
        coalesce_seconds=coalesce_seconds,
        error_backoff_seconds=0.0,
    )


async def _wait_until(cond: Callable[[], bool], *, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


# ── message helpers ──────────────────────────────────────────────────────────────


def test_depth_message_round_trips() -> None:
    assert parse_depth_message(depth_message(0)) == 0
    assert parse_depth_message(depth_message(42)) == 42


def test_parse_depth_message_rejects_malformed_or_negative() -> None:
    assert parse_depth_message("not json") is None
    assert parse_depth_message("{}") is None
    assert parse_depth_message('{"queue_depth": -1}') is None
    assert parse_depth_message('{"queue_depth": "7"}') is None
    assert parse_depth_message('{"queue_depth": true}') is None  # bool is not a count


async def test_publisher_publishes_depth_message_on_the_channel() -> None:
    redis = _FakeRedis()
    publish = redis_queue_depth_publisher(cast(Any, redis), CHANNEL)
    await publish(5)
    assert redis.published == [(CHANNEL, depth_message(5))]


# ── _Coalescer policy (fake clock, deterministic) ──────────────────────────────────


def test_coalescer_first_offer_emits_immediately() -> None:
    now = [0.0]
    c = _Coalescer(window=1.0, clock=lambda: now[0])
    assert c.offer(5) == (5, None)


def test_coalescer_second_offer_in_window_schedules_latest_wins() -> None:
    now = [0.0]
    c = _Coalescer(window=1.0, clock=lambda: now[0])
    assert c.offer(5) == (5, None)  # emits now, window starts at t=0
    now[0] = 0.3
    emit, delay = c.offer(6)  # within window -> schedule a flush for the remainder
    assert emit is None and delay is not None and abs(delay - 0.7) < 1e-9
    now[0] = 0.5
    assert c.offer(7) == (None, None)  # flush already scheduled; just updates pending
    now[0] = 1.0
    assert c.flush() == 7  # the LATEST wins; 6 was coalesced away


def test_coalescer_emits_again_once_the_window_has_passed() -> None:
    now = [0.0]
    c = _Coalescer(window=1.0, clock=lambda: now[0])
    assert c.offer(5) == (5, None)
    now[0] = 1.5  # a full window later
    assert c.offer(6) == (6, None)  # emits immediately, no scheduling


def test_coalescer_flush_with_no_pending_returns_none() -> None:
    c = _Coalescer(window=1.0, clock=lambda: 0.0)
    assert c.flush() is None


# ── broadcaster fan-out (coalesce window 0 -> every frame emits immediately) ────────


async def test_start_subscribes_once_and_is_idempotent() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    await b.start()
    await b.start()
    try:
        assert redis.pubsub_calls == 1
        assert redis.pubsub_obj.channels == {CHANNEL}
    finally:
        await b.stop()


async def test_relay_fans_a_depth_out_to_every_registered_queue() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    q1 = b.register()
    q2 = b.register()
    await b.start()
    try:
        redis.pubsub_obj.feed(depth_message(3))
        assert await _wait_until(lambda: not q1.empty() and not q2.empty())
        assert q1.get_nowait() == 3
        assert q2.get_nowait() == 3
    finally:
        await b.stop()


async def test_malformed_frame_is_skipped_and_a_later_valid_frame_still_delivers() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    q = b.register()
    await b.start()
    try:
        redis.pubsub_obj.feed("this is not json")
        redis.pubsub_obj.feed(depth_message(7))
        assert await _wait_until(lambda: not q.empty())
        assert q.get_nowait() == 7
        assert q.empty()
    finally:
        await b.stop()


async def test_relay_survives_get_message_error_then_resumes() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    q = b.register()
    redis.pubsub_obj.raise_times = 2
    await b.start()
    try:
        redis.pubsub_obj.feed(depth_message(9))
        assert await _wait_until(lambda: not q.empty())
        assert q.get_nowait() == 9
    finally:
        await b.stop()


async def test_coalescing_drops_the_middle_update_and_delivers_latest() -> None:
    """End-to-end: with a real (small) window, a burst collapses to the first emit plus
    one latest-wins flush -- the middle value never reaches the client."""
    redis = _FakeRedis()
    b = _make(redis, coalesce_seconds=0.2)
    q = b.register()
    await b.start()
    try:
        redis.pubsub_obj.feed(depth_message(5))  # emits immediately
        assert await _wait_until(lambda: not q.empty())
        assert q.get_nowait() == 5
        redis.pubsub_obj.feed(depth_message(6))  # within window -> pending
        redis.pubsub_obj.feed(depth_message(7))  # still within window -> latest pending
        assert await _wait_until(lambda: not q.empty(), timeout=1.0)
        assert q.get_nowait() == 7  # the flush delivered the LATEST, not 6
        # 6 was coalesced away: nothing else is queued.
        await asyncio.sleep(0.05)
        assert q.empty()
    finally:
        await b.stop()


async def test_register_unregister_tracks_queue_set() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    q = b.register()
    assert len(b._queues) == 1
    b.unregister(q)
    assert len(b._queues) == 0
    b.unregister(q)  # safe to repeat
    assert len(b._queues) == 0


async def test_stop_cancels_task_and_closes_subscription() -> None:
    redis = _FakeRedis()
    b = _make(redis)
    await b.start()
    assert b._task is not None
    await b.stop()
    assert b._task is None
    assert redis.pubsub_obj.closed is True
    await b.stop()  # idempotent
