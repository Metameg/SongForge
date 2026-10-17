"""Unit tests for `PointerBroadcaster` in isolation (issue #10, criterion #2).

The `/events` edge tests (`test_events_sse.py`) exercise the broadcaster indirectly
through a live SSE connection; these drive it directly against a fake Redis pub/sub
double so the paths a real Redis rarely triggers on demand — a full client queue, a
malformed frame, a `get_message` error, start/stop lifecycle — are each pinned. Kept on
the fast unit path (no socket, no live datastore), per the PRD testing decision to keep
decision/robustness logic unit-testable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from songforge.metrics import sse_connected_listeners
from songforge.radio.pointer_broadcaster import _QUEUE_MAXSIZE, PointerBroadcaster
from songforge.radio.pointer_cache import PointerCache, PointerRecord

CHANNEL = "radio:pointer:changed"


def _record(*, song_id: str = "song-1", version: int = 1, playback_id: str = "pb-1") -> PointerRecord:
    now = datetime.now(timezone.utc)
    return PointerRecord(
        song_id=song_id,
        title=f"Title for {song_id}",
        source="static",
        object_key=f"audio/{song_id}.mp3",
        audio_url=f"http://cdn.test/audio/{song_id}.mp3",
        started_at=(now - timedelta(seconds=5)).isoformat(),
        ends_at=(now + timedelta(seconds=175)).isoformat(),
        duration_seconds=180,
        playback_id=playback_id,
        version=version,
    )


class _FakePubSub:
    """Mirrors the `subscribe`/`get_message`/`close` surface the relay loop uses.

    `feed()` enqueues a raw pub/sub frame the loop will read; `raise_times` makes the
    next N `get_message` calls raise (to drive the error-backoff path). `get_message`
    with `timeout=None` blocks on the internal queue, matching real redis-py semantics.
    """

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
    """Hands out one shared `_FakePubSub` and counts `pubsub()` calls (idempotency)."""

    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub()
        self.pubsub_calls = 0

    def pubsub(self) -> _FakePubSub:
        self.pubsub_calls += 1
        return self.pubsub_obj


def _make(
    redis: _FakeRedis, *, error_backoff_seconds: float = 0.0
) -> tuple[PointerBroadcaster, PointerCache]:
    cache = PointerCache(ttl_seconds=60.0)
    broadcaster = PointerBroadcaster(
        redis=cast(Any, redis),
        channel=CHANNEL,
        cache=cache,
        error_backoff_seconds=error_backoff_seconds,
    )
    return broadcaster, cache


async def _wait_until(cond: Callable[[], bool], *, timeout: float = 1.0) -> bool:
    """Poll `cond` cooperatively until true or `timeout` — lets the relay task run."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


async def test_start_subscribes_exactly_once_and_is_idempotent() -> None:
    redis = _FakeRedis()
    broadcaster, _cache = _make(redis)
    await broadcaster.start()
    await broadcaster.start()  # second call is a no-op
    try:
        assert redis.pubsub_calls == 1
        assert redis.pubsub_obj.channels == {CHANNEL}
    finally:
        await broadcaster.stop()


async def test_relay_updates_cache_and_fans_out_to_every_registered_queue() -> None:
    redis = _FakeRedis()
    broadcaster, cache = _make(redis)
    q1 = broadcaster.register()
    q2 = broadcaster.register()
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(_record(song_id="song-2", version=2).to_json())
        assert await _wait_until(lambda: not q1.empty() and not q2.empty())
        assert q1.get_nowait().song_id == "song-2"
        assert q2.get_nowait().song_id == "song-2"
        cached = cache.get()
        assert cached is not None and cached.song_id == "song-2" and cached.version == 2
    finally:
        await broadcaster.stop()


async def test_full_client_queue_drops_the_frame_without_starving_others() -> None:
    redis = _FakeRedis()
    broadcaster, cache = _make(redis)
    full = broadcaster.register()
    for _ in range(_QUEUE_MAXSIZE):  # saturate one client's queue
        full.put_nowait(_record(song_id="old"))
    empty = broadcaster.register()
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(_record(song_id="fresh", version=9).to_json())
        # The empty client still receives it, the loop survives the QueueFull...
        assert await _wait_until(lambda: not empty.empty())
        assert empty.get_nowait().song_id == "fresh"
        # ...the full client's queue stayed capped (frame dropped, not appended)...
        assert full.qsize() == _QUEUE_MAXSIZE
        # ...and the cache was updated before fan-out, so the drop lost nothing durable.
        cached = cache.get()
        assert cached is not None and cached.song_id == "fresh"
    finally:
        await broadcaster.stop()


async def test_malformed_frame_is_skipped_and_a_later_valid_frame_still_delivers() -> None:
    redis = _FakeRedis()
    broadcaster, cache = _make(redis)
    q = broadcaster.register()
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed("this is not json")  # must not kill the loop
        redis.pubsub_obj.feed(_record(song_id="valid", version=3).to_json())
        assert await _wait_until(lambda: not q.empty())
        delivered = q.get_nowait()
        assert delivered.song_id == "valid"  # only the valid frame was fanned out
        assert q.empty()
        cached = cache.get()
        assert cached is not None and cached.song_id == "valid"
    finally:
        await broadcaster.stop()


async def test_non_message_frame_is_ignored() -> None:
    redis = _FakeRedis()
    broadcaster, cache = _make(redis)
    q = broadcaster.register()
    await broadcaster.start()
    try:
        # A subscribe-confirmation frame (not a real message) must not touch cache/queue.
        redis.pubsub_obj.feed(_record(song_id="ignored").to_json(), msg_type="subscribe")
        redis.pubsub_obj.feed(_record(song_id="real", version=4).to_json())
        assert await _wait_until(lambda: not q.empty())
        assert q.get_nowait().song_id == "real"
        assert q.empty()
        assert cache.get() is not None and cache.get().song_id == "real"  # type: ignore[union-attr]
    finally:
        await broadcaster.stop()


async def test_relay_loop_survives_get_message_error_then_resumes() -> None:
    redis = _FakeRedis()
    broadcaster, cache = _make(redis, error_backoff_seconds=0.0)
    q = broadcaster.register()
    redis.pubsub_obj.raise_times = 2  # first two reads raise (Redis blip), then recover
    await broadcaster.start()
    try:
        redis.pubsub_obj.feed(_record(song_id="recovered", version=5).to_json())
        assert await _wait_until(lambda: not q.empty())
        assert q.get_nowait().song_id == "recovered"
        cached = cache.get()
        assert cached is not None and cached.song_id == "recovered"
    finally:
        await broadcaster.stop()


async def test_register_unregister_tracks_queue_set_and_gauge() -> None:
    redis = _FakeRedis()
    broadcaster, _cache = _make(redis)
    baseline = sse_connected_listeners._value.get()

    q = broadcaster.register()
    assert len(broadcaster._queues) == 1
    assert sse_connected_listeners._value.get() == baseline + 1

    broadcaster.unregister(q)
    assert len(broadcaster._queues) == 0
    assert sse_connected_listeners._value.get() == baseline

    # Unregistering an already-removed queue must not double-decrement the gauge.
    broadcaster.unregister(q)
    assert sse_connected_listeners._value.get() == baseline


async def test_stop_cancels_the_relay_task_and_closes_the_subscription() -> None:
    redis = _FakeRedis()
    broadcaster, _cache = _make(redis)
    await broadcaster.start()
    assert broadcaster._task is not None
    await broadcaster.stop()
    assert broadcaster._task is None
    assert redis.pubsub_obj.closed is True
    # Idempotent: a second stop after everything is torn down must not raise.
    await broadcaster.stop()
