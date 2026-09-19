"""Per-instance Redis pub/sub relay -> in-process SSE fan-out (issue #10, criterion #2).

`GET /events` (`web/routes/events.py`) needs every connected listener to learn about a
song change the moment the coordinator advances, without each listener adding its own
Redis subscription — that would multiply Redis load with listener count, defeating the
whole "listener count decoupled from datastore load" thesis (PRD stories #64/#65).

`PointerBroadcaster` is the fix: ONE `redis.pubsub()` subscription per app instance,
started once at app startup (`start()`, wired through the FastAPI `lifespan` in
`web/app.py`), whose listener loop (`_relay_loop`) receives each published pointer and
fans it out to every connected client's own bounded `asyncio.Queue` (`register()`/
`unregister()`). On each received message it also updates the app's `PointerCache`
(issue #9's documented `invalidate()`/`set()` seam) *before* fan-out, so `/now-playing`
reflects the change immediately rather than after the cache's TTL.

Construction does no I/O — only `start()` touches Redis — so `create_app()` stays
importable/testable with no live datastore (mirrors `PointerCache`'s construction, and
`Redis.from_url(...)` in `redis_client.py`, which doesn't connect until first use).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from songforge.logging_setup import get_logger
from songforge.metrics import radio_pointer_events_relayed_total, sse_connected_listeners
from songforge.radio.pointer_cache import PointerCache, PointerRecord

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

log = get_logger(__name__)

# Small bound: a stalled/slow client must not leak memory. A dropped frame is
# harmless -- the client self-heals on the next heartbeat re-emit or on reconnect
# (both re-send the *current* pointer, not a queued backlog).
_QUEUE_MAXSIZE = 8

# Sleep after an unexpected `get_message` error before retrying, so a Redis outage
# (which makes `get_message` raise immediately and repeatedly) degrades into a slow
# retry loop rather than a CPU hot-spin + log flood. Short enough that recovery is
# prompt once Redis returns; a genuine push arriving during the sleep is not lost --
# it is still queued on the subscription and picked up on the next successful read.
_ERROR_BACKOFF_SECONDS = 0.5


class PointerBroadcaster:
    """Fans out one Redis pub/sub subscription to N per-client queues.

    Not started at construction (no I/O) -- call `start()` once the app's event loop is
    running (the FastAPI `lifespan`), and `stop()` on shutdown. Both are idempotent-safe
    to call from a lifespan context manager.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        channel: str,
        cache: PointerCache,
        error_backoff_seconds: float = _ERROR_BACKOFF_SECONDS,
    ) -> None:
        self._redis = redis
        self._channel = channel
        self._cache = cache
        self._error_backoff_seconds = error_backoff_seconds
        self._pubsub: PubSub | None = None
        self._task: asyncio.Task[None] | None = None
        self._queues: set[asyncio.Queue[PointerRecord]] = set()

    def register(self) -> asyncio.Queue[PointerRecord]:
        """Register a new SSE client, returning its bounded delivery queue."""
        queue: asyncio.Queue[PointerRecord] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues.add(queue)
        sse_connected_listeners.inc()
        return queue

    def unregister(self, queue: asyncio.Queue[PointerRecord]) -> None:
        """Deregister a client's queue. Safe to call even if already removed."""
        if queue in self._queues:
            self._queues.discard(queue)
            sse_connected_listeners.dec()

    async def start(self) -> None:
        """Subscribe to the pub/sub channel and start the background relay loop.

        Idempotent: a second call while already started is a no-op.
        """
        if self._task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self._channel)
        self._task = asyncio.create_task(self._relay_loop())

    async def stop(self) -> None:
        """Cancel the relay loop and close the subscription, best-effort.

        Mirrors this codebase's Redis-outage convention (log + swallow) -- shutdown
        must never raise because Redis happened to be unreachable by then.
        """
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.close()
            except Exception:
                log.warning("pointer_broadcaster_close_failed", exc_info=True)
            self._pubsub = None

    async def _relay_loop(self) -> None:
        """Pull messages off the subscription and fan each one out in-process.

        `timeout=None` blocks until a message arrives (real redis-py semantics; the
        test double mirrors it) rather than busy-polling. Wrapped per-message in its
        own try/except so one malformed frame never kills the loop -- log and continue,
        same "a bad frame is not the app's problem" posture as the rest of the
        Redis-facing code.
        """
        assert self._pubsub is not None  # noqa: S101 -- set by start() just above
        pubsub = self._pubsub
        while True:
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis unreachable (or the subscription dropped): `get_message` will
                # keep raising until it recovers. Back off before retrying so this
                # degrades into a slow retry loop, not a hot-spin + log flood.
                log.warning("pointer_broadcaster_get_message_failed", exc_info=True)
                await asyncio.sleep(self._error_backoff_seconds)
                continue
            if message is None or message.get("type") != "message":
                continue
            try:
                record = PointerRecord.from_json(message["data"])
            except Exception:
                log.warning("pointer_broadcaster_malformed_message", exc_info=True)
                continue

            self._cache.set(record)
            for queue in list(self._queues):
                try:
                    queue.put_nowait(record)
                    radio_pointer_events_relayed_total.inc()
                except asyncio.QueueFull:
                    log.warning("pointer_broadcaster_queue_full_dropped_frame")
