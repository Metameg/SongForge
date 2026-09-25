"""Per-user SSE notification fan-out (issue #16, acceptance criterion A4).

`jobs.watchdog.sweep_terminal_failures` needs to tell exactly one user "your song
failed, your quota slot was refunded" over their own `/events` connection -- without
adding one Redis subscription per connected client, which would defeat the same
"listener count must not multiply Redis load" thesis `radio.pointer_broadcaster.
PointerBroadcaster` was built for (see that module's docstring). `UserEventBroadcaster`
is the per-user mirror: ONE Redis PSUBSCRIBE per app instance against a wildcard
pattern (`{prefix}*:events`, e.g. `user:*:events`), routing each received message only
to the per-client queues registered under the user id extracted from its channel name
(`{prefix}{user_id}:events`) -- `register()`/`unregister()` keyed by `user_id`, unlike
`PointerBroadcaster`'s single flat queue set.

Construction does no I/O (mirrors `PointerBroadcaster`) so `create_app()` stays
importable/testable with no live datastore.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis

from songforge.logging_setup import get_logger
from songforge.metrics import user_notifications_relayed_total

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

log = get_logger(__name__)

# Small bound, mirrors `PointerBroadcaster._QUEUE_MAXSIZE`: a stalled/slow client must
# not leak memory. A dropped frame is harmless here too -- worst case a user misses one
# `job-failed` push and discovers the outcome on their next `/create` or a page refresh
# (there is no replay/backlog contract for this channel, unlike the radio pointer).
_QUEUE_MAXSIZE = 8

# Mirrors `PointerBroadcaster._ERROR_BACKOFF_SECONDS`: backs off a Redis-outage hot
# loop instead of busy-spinning + flooding logs.
_ERROR_BACKOFF_SECONDS = 0.5


def user_channel(prefix: str, user_id: str) -> str:
    """The Redis pub/sub channel one user's events are published to."""
    return f"{prefix}{user_id}:events"


def channel_pattern(prefix: str) -> str:
    """The PSUBSCRIBE wildcard pattern matching every user's channel."""
    return f"{prefix}*:events"


def extract_user_id(channel: str, *, prefix: str) -> str | None:
    """Recover `user_id` from a channel name matching `user_channel`'s shape, or
    `None` if `channel` doesn't match that shape (defensive -- a malformed/foreign
    channel delivered on the same wildcard pattern must never crash the relay loop)."""
    suffix = ":events"
    if not channel.startswith(prefix) or not channel.endswith(suffix):
        return None
    user_id = channel[len(prefix) : -len(suffix)]
    return user_id or None


class UserEventBroadcaster:
    """Fans out one Redis PSUBSCRIBE to N per-user, per-client queues.

    Not started at construction (no I/O) -- call `start()` once the app's event loop
    is running (the FastAPI `lifespan`), and `stop()` on shutdown. Both are
    idempotent-safe to call from a lifespan context manager.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        channel_prefix: str,
        error_backoff_seconds: float = _ERROR_BACKOFF_SECONDS,
    ) -> None:
        self._redis = redis
        self._channel_prefix = channel_prefix
        self._error_backoff_seconds = error_backoff_seconds
        self._pubsub: PubSub | None = None
        self._task: asyncio.Task[None] | None = None
        self._queues: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def register(self, user_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client for `user_id`, returning its bounded queue."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues.setdefault(user_id, set()).add(queue)
        return queue

    def unregister(self, user_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Deregister a client's queue. Safe to call even if already removed."""
        queues = self._queues.get(user_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._queues.pop(user_id, None)

    async def start(self) -> None:
        """PSUBSCRIBE to the wildcard pattern and start the background relay loop.

        Idempotent: a second call while already started is a no-op.
        """
        if self._task is not None:
            return
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(channel_pattern(self._channel_prefix))
        self._task = asyncio.create_task(self._relay_loop())

    async def stop(self) -> None:
        """Cancel the relay loop and close the subscription, best-effort.

        Mirrors `PointerBroadcaster.stop`'s Redis-outage convention (log + swallow) --
        shutdown must never raise because Redis happened to be unreachable by then.
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
                log.warning("user_event_broadcaster_close_failed", exc_info=True)
            self._pubsub = None

    async def _relay_loop(self) -> None:
        """Pull pattern-subscribed frames off the subscription and fan each one out
        to the user it belongs to, in-process (mirrors
        `PointerBroadcaster._relay_loop`'s error-backoff/per-message-try-except
        conventions, but keyed by the user id extracted from the frame's channel
        rather than a single flat queue set).
        """
        assert self._pubsub is not None  # noqa: S101 -- set by start() just above
        pubsub = self._pubsub
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=None
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Redis unreachable (or the subscription dropped): `get_message` will
                # keep raising until it recovers. Back off before retrying so this
                # degrades into a slow retry loop, not a hot-spin + log flood.
                log.warning("user_event_broadcaster_get_message_failed", exc_info=True)
                await asyncio.sleep(self._error_backoff_seconds)
                continue
            if message is None or message.get("type") != "pmessage":
                continue

            user_id = extract_user_id(message["channel"], prefix=self._channel_prefix)
            if user_id is None:
                log.warning(
                    "user_event_broadcaster_unmatched_channel",
                    channel=message.get("channel"),
                )
                continue

            try:
                payload = json.loads(message["data"])
            except Exception:
                log.warning("user_event_broadcaster_malformed_message", exc_info=True)
                continue

            for queue in list(self._queues.get(user_id, ())):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    log.warning("user_event_broadcaster_queue_full_dropped_frame")
                    continue
                # Delivery-side counterpart to `metrics.user_notifications_total`
                # (the publish-side count) -- mirrors `PointerBroadcaster`'s
                # `radio_pointer_events_relayed_total`: grows with listener count x
                # pushes actually handed to a connected client's queue, excluding
                # drops (F6).
                user_notifications_relayed_total.inc()
