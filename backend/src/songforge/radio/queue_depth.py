"""Per-instance Redis pub/sub relay for the GLOBAL waiting-queue depth (depth-in-SSE
follow-up to issue #10).

`song-change` carries `queue_depth` only at advance time, so a listener's "N waiting"
goes stale between advances -- it does NOT update as newly generated songs are enqueued
onto `playback_queue`. `QueueDepthBroadcaster` closes that gap, mirroring
`radio.pointer_broadcaster.PointerBroadcaster`: ONE Redis subscription per app instance
on `settings.radio_queue_depth_channel`, fanning each published depth out in-process to
every connected `/events` client as a `queue-depth` frame -- so the listener count stays
decoupled from Redis/Postgres load exactly as the pointer relay does (no per-client
subscription, no per-client count query).

Fan-out is COALESCED to at most one emit per `coalesce_seconds` (default 1s) PER APP
INSTANCE: a burst of enqueues within the window collapses to a single latest-wins push,
so the per-client event rate is bounded no matter how fast songs are enqueued. The
coalescing policy (`_Coalescer`) is a pure, clock-injected object so it is unit-testable
without real time; the relay only wires it to `loop.call_later`.

Construction does no I/O (mirrors `PointerBroadcaster`/`UserEventBroadcaster`) so
`create_app()` stays importable/testable with no live datastore.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from songforge.logging_setup import get_logger
from songforge.metrics import queue_depth_events_relayed_total

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

log = get_logger(__name__)

# Mirror `PointerBroadcaster._QUEUE_MAXSIZE`: a slow client must not leak memory; a
# dropped depth frame is harmless -- depth is advisory and the next push (or the next
# `song-change`) re-sends the current value, with no replay/backlog contract.
_QUEUE_MAXSIZE = 8

# Mirror `PointerBroadcaster._ERROR_BACKOFF_SECONDS`: back off a Redis-outage hot loop.
_ERROR_BACKOFF_SECONDS = 0.5

# Default coalesce window: at most one `queue-depth` push per second per app instance.
_COALESCE_SECONDS = 1.0


# Publishes one depth onto the global channel: ``publish(depth)``.
PublishQueueDepthFn = Callable[[int], Awaitable[None]]


def depth_message(depth: int) -> str:
    """Wire JSON for a depth push. Keyed `queue_depth` so the SSE `queue-depth` frame
    body matches the `song-change` frame's depth field -- the frontend parses both with
    the same `parseDepthFromSongChange`."""
    return json.dumps({"queue_depth": depth})


def parse_depth_message(data: str) -> int | None:
    """Recover the depth from a `depth_message`, or `None` if the frame is malformed /
    carries no usable non-negative integer depth (defensive: a bad frame on the channel
    must never crash the relay)."""
    try:
        value = json.loads(data).get("queue_depth")
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def redis_queue_depth_publisher(redis: Redis, channel: str) -> PublishQueueDepthFn:
    """The real publisher: ``PUBLISH`` the depth onto the global channel."""

    async def _publish(depth: int) -> None:
        await redis.publish(channel, depth_message(depth))

    return _publish


async def safe_publish_queue_depth(
    publish: PublishQueueDepthFn, depth: int, *, log_event: str
) -> None:
    """Best-effort post-commit publish: a Redis blip must never abort or undo an
    already-committed enqueue, so errors are logged and swallowed (mirrors
    `user_events.safe_publish_user_event`)."""
    try:
        await publish(depth)
    except Exception:
        log.exception(log_event)


class _Coalescer:
    """Pure latest-wins rate limiter: at most one emit per `window` seconds.

    `offer(depth)` records the newest depth and returns `(emit_now, schedule_after)`:
    `emit_now` is a depth to fan out immediately (and marks the window started);
    `schedule_after` is a delay after which the caller must invoke `flush()` to emit the
    pending latest value. Exactly one of them is ever non-None (or both None when a flush
    is already scheduled -- the pending value is simply updated). `flush()` returns the
    pending depth to emit (or None). Clock-injected so tests drive it without real time.
    """

    def __init__(self, *, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window
        self._clock = clock
        self._last_emit: float | None = None
        self._pending: int | None = None
        self._flush_scheduled = False

    def offer(self, depth: int) -> tuple[int | None, float | None]:
        now = self._clock()
        self._pending = depth
        if self._flush_scheduled:
            return None, None
        if self._last_emit is None or (now - self._last_emit) >= self._window:
            self._pending = None
            self._last_emit = now
            return depth, None
        self._flush_scheduled = True
        return None, self._window - (now - self._last_emit)

    def flush(self) -> int | None:
        self._flush_scheduled = False
        if self._pending is None:
            return None
        depth = self._pending
        self._pending = None
        self._last_emit = self._clock()
        return depth


class QueueDepthBroadcaster:
    """Fans out one Redis subscription to N per-client queues, coalesced to <=1/window.

    Not started at construction (no I/O) -- call `start()` once the event loop is running
    (the FastAPI `lifespan`) and `stop()` on shutdown; both are idempotent-safe.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        channel: str,
        coalesce_seconds: float = _COALESCE_SECONDS,
        error_backoff_seconds: float = _ERROR_BACKOFF_SECONDS,
    ) -> None:
        self._redis = redis
        self._channel = channel
        self._coalesce_seconds = coalesce_seconds
        self._error_backoff_seconds = error_backoff_seconds
        self._pubsub: PubSub | None = None
        self._task: asyncio.Task[None] | None = None
        self._queues: set[asyncio.Queue[int]] = set()
        self._coalescer = _Coalescer(window=coalesce_seconds)
        self._flush_handle: asyncio.TimerHandle | None = None

    def register(self) -> asyncio.Queue[int]:
        """Register a new SSE client, returning its bounded depth queue. Does NOT touch
        `sse_connected_listeners` -- the pointer broadcaster already counts the client."""
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue[int]) -> None:
        """Deregister a client's queue. Safe to call even if already removed."""
        self._queues.discard(queue)

    async def start(self) -> None:
        """Subscribe to the channel and start the background relay loop (idempotent)."""
        if self._task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._task = asyncio.create_task(self._relay_loop())

    async def stop(self) -> None:
        """Cancel the relay loop + any pending flush and close the subscription,
        best-effort (mirrors `PointerBroadcaster.stop`'s log-and-swallow convention)."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.close()
            except Exception:
                log.warning("queue_depth_broadcaster_close_failed", exc_info=True)
            self._pubsub = None

    def _emit(self, depth: int) -> None:
        """Fan one depth out to every registered client queue (drop on a full queue)."""
        for queue in list(self._queues):
            try:
                queue.put_nowait(depth)
                queue_depth_events_relayed_total.inc()
            except asyncio.QueueFull:
                log.warning("queue_depth_broadcaster_queue_full_dropped_frame")

    def _scheduled_flush(self) -> None:
        self._flush_handle = None
        depth = self._coalescer.flush()
        if depth is not None:
            self._emit(depth)

    def _accept(self, depth: int) -> None:
        """Run `depth` through the coalescer and emit now or schedule a flush."""
        emit_now, schedule_after = self._coalescer.offer(depth)
        if emit_now is not None:
            self._emit(emit_now)
        elif schedule_after is not None and self._flush_handle is None:
            loop = asyncio.get_event_loop()
            self._flush_handle = loop.call_later(schedule_after, self._scheduled_flush)

    async def _relay_loop(self) -> None:
        """Pull depth frames off the subscription and feed each to the coalescer
        (mirrors `PointerBroadcaster._relay_loop`'s error-backoff + per-message
        try/except conventions)."""
        assert self._pubsub is not None  # noqa: S101 -- set by start() just above
        pubsub = self._pubsub
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("queue_depth_broadcaster_get_message_failed", exc_info=True)
                await asyncio.sleep(self._error_backoff_seconds)
                continue
            if message is None or message.get("type") != "message":
                continue
            depth = parse_depth_message(message["data"])
            if depth is None:
                log.warning("queue_depth_broadcaster_malformed_message")
                continue
            self._accept(depth)
