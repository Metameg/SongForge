"""``GET /events`` — SSE push of the radio pointer (issue #10, criteria #1, #2, #4).

Plain `StreamingResponse` wrapping a hand-built `text/event-stream` generator -- no new
dependency (`sse-starlette` etc.), since the wire format is three lines per event and
reconnect-truth is satisfied by re-sending the current pointer as the first frame of
every new connection rather than SSE `Last-Event-ID` replay.

On connect, the current pointer is resolved through the exact same
`get_now_playing_cached` chain `/now-playing` uses (so the on-connect frame is as fresh/
consistent as a `/now-playing` read would be), then sent immediately as `event:
song-change` -- or `event: idle` if the station has no pointer at all yet, symmetric
with `/now-playing`'s 503 idle body. After that, the generator blocks on this client's
own `PointerBroadcaster` queue (fed by the app's single per-instance pub/sub
subscription, see `radio/pointer_broadcaster.py`) and re-emits the current pointer
either when a genuinely new one arrives, or every `sse_heartbeat_seconds` with no new
message (criterion #4's heartbeat re-sync) -- the heartbeat path never re-touches
Redis/Postgres/the cache, it only re-serializes the value already held locally.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from songforge.config import get_settings
from songforge.logging_setup import get_logger
from songforge.radio.pointer_broadcaster import PointerBroadcaster
from songforge.radio.pointer_cache import (
    PgLoader,
    PointerCache,
    PointerRecord,
    get_now_playing_cached,
)
from songforge.radio.queue_depth import QueueDepthBroadcaster
from songforge.radio.user_events import UserEventBroadcaster
from songforge.web.identity import resolve_identity
from songforge.web.routes.now_playing import (
    get_pg_loader_dependency,
    get_pointer_cache,
    get_redis_dependency,
    get_session,
)

router = APIRouter(tags=["radio"])
log = get_logger(__name__)


def get_heartbeat_seconds() -> float:
    """The ~30s heartbeat interval; overridable in tests to shrink it."""
    return get_settings().sse_heartbeat_seconds


def get_pointer_broadcaster(request: Request) -> PointerBroadcaster:
    """This app instance's `PointerBroadcaster` (set once per `create_app()` call)."""
    return cast(PointerBroadcaster, request.app.state.pointer_broadcaster)


def get_user_event_broadcaster(request: Request) -> UserEventBroadcaster:
    """This app instance's `UserEventBroadcaster` (issue #16, criterion A4): the
    per-user mirror of `get_pointer_broadcaster`, set once per `create_app()` call."""
    return cast(UserEventBroadcaster, request.app.state.user_event_broadcaster)


def get_queue_depth_broadcaster(request: Request) -> QueueDepthBroadcaster:
    """This app instance's `QueueDepthBroadcaster` (depth-in-SSE follow-up): the GLOBAL
    mirror of `get_pointer_broadcaster`, set once per `create_app()` call."""
    return cast(QueueDepthBroadcaster, request.app.state.queue_depth_broadcaster)


_USER_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "job-failed": ("job_id",),
    "job-progress": ("job_id", "state", "eta"),
    "job-ready": ("job_id", "song_id", "title"),
}


def _format_user_event(message: dict[str, Any]) -> str | None:
    """SSE frame for a per-user message, named by its `event` key. A message with NO
    `event` key is a legacy `job-failed`; a message whose `event` is present but unknown
    returns `None` (dropped) so a future/malformed event never masquerades as a failure.
    Fields are whitelisted so no stray identity data can leak onto the wire."""
    name = message["event"] if "event" in message else "job-failed"
    fields = _USER_EVENT_FIELDS.get(name) if isinstance(name, str) else None
    if fields is None:
        return None
    return _format_event(name, {f: message.get(f) for f in fields})


def _format_event(event: str, payload: dict[str, Any]) -> str:
    """Hand-build one `text/event-stream` frame: a named event + a JSON data line."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _song_change_payload(
    record: PointerRecord,
    *,
    server_time: datetime,
    include_queue_depth: bool = True,
    freshest_depth: int | None = None,
) -> dict[str, Any]:
    """The `song-change` SSE frame body: the pure `/now-playing` view PLUS `queue_depth`
    (depth-in-SSE). Depth is stamped onto the pointer by the coordinator at the LAST
    advance, so it travels for free on a genuine connect/push -- but it goes stale
    between advances as the `queue-depth` broadcaster (worker-side enqueues) keeps
    pushing fresher values. Two ways this is handled:

    - `include_queue_depth=False` (the heartbeat re-emit's only caller) sends `null`
      instead of the pointer's stale number.
    - `freshest_depth` (the CONNECT frame's only caller, via
      `QueueDepthBroadcaster.last_known_depth()`) overrides `record.queue_depth` with
      whatever this app instance has seen most recently on the depth channel, when
      available -- a brand-new connection (or a page refresh) would otherwise always
      start from the last-advance snapshot, which can be visibly behind reality if the
      radio hasn't advanced in a while even though songs kept getting enqueued.

    Either way, the frontend's `parseDepthFromSongChange`/`onDepth` already treat a
    null/absent depth as "keep the last value," so `include_queue_depth=False` never
    resets the displayed count -- it just stops a stale re-send from clobbering it.
    Added HERE, not in `PointerRecord.to_response`, so that body stays byte-identical to
    the `/now-playing` contract."""
    if not include_queue_depth:
        depth = None
    elif freshest_depth is not None:
        depth = freshest_depth
    else:
        depth = record.queue_depth
    return {**record.to_response(server_time=server_time), "queue_depth": depth}


@router.get("/events")
async def events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis_dependency),
    pg_loader: PgLoader = Depends(get_pg_loader_dependency),
    cache: PointerCache = Depends(get_pointer_cache),
    broadcaster: PointerBroadcaster = Depends(get_pointer_broadcaster),
    user_broadcaster: UserEventBroadcaster = Depends(get_user_event_broadcaster),
    depth_broadcaster: QueueDepthBroadcaster = Depends(get_queue_depth_broadcaster),
    heartbeat_seconds: float = Depends(get_heartbeat_seconds),
) -> StreamingResponse:
    """Stream `song-change` events (the current pointer on connect, then every push)
    ALONGSIDE this caller's own `job-failed` notifications (issue #16, criterion A4;
    design D4: no new endpoint). Identity is only READ here (`resolve_identity`) --
    this GET never mints/sets the identity cookie, unlike `POST /create`; a listener
    with no cookie yet simply never receives a `job-failed` frame."""
    settings = get_settings()
    current: PointerRecord | None = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=session,
        pg_loader=pg_loader,
        redis_key=settings.radio_pointer_redis_key,
    )
    queue = broadcaster.register()
    identity = resolve_identity(request, settings)
    user_queue = user_broadcaster.register(identity.user_id)
    depth_queue = depth_broadcaster.register()

    async def stream() -> AsyncIterator[str]:
        nonlocal current
        pointer_task: asyncio.Task[PointerRecord] | None = None
        user_task: asyncio.Task[dict[str, Any]] | None = None
        depth_task: asyncio.Task[int] | None = None
        try:
            if current is None:
                yield _format_event("idle", {"status": "idle"})
            else:
                # Prefer the freshest depth this instance has seen on the depth
                # channel over the pointer's last-advance snapshot -- fixes the
                # connect/refresh dip where a new connection would otherwise always
                # start from a stale number until the next advance or broadcast.
                yield _format_event(
                    "song-change",
                    _song_change_payload(
                        current,
                        server_time=datetime.now(timezone.utc),
                        freshest_depth=depth_broadcaster.last_known_depth(),
                    ),
                )

            while True:
                if await request.is_disconnected():
                    break
                if pointer_task is None:
                    pointer_task = asyncio.ensure_future(queue.get())
                if user_task is None:
                    user_task = asyncio.ensure_future(user_queue.get())
                if depth_task is None:
                    depth_task = asyncio.ensure_future(depth_queue.get())

                done, _pending = await asyncio.wait(
                    {pointer_task, user_task, depth_task},
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    # Heartbeat: re-emit the last-known pointer, held locally -- this
                    # is the one place a naive implementation would silently
                    # reintroduce per-listener datastore load on every heartbeat.
                    # Never re-sends a job-failed frame (one-shot, no replay).
                    # queue_depth is omitted (null): it would otherwise be the STALE
                    # depth from the last advance, clobbering whatever fresher value
                    # the queue-depth broadcaster has pushed since (see
                    # _song_change_payload).
                    if current is not None:
                        yield _format_event(
                            "song-change",
                            _song_change_payload(
                                current,
                                server_time=datetime.now(timezone.utc),
                                include_queue_depth=False,
                            ),
                        )
                    continue

                if pointer_task in done:
                    current = pointer_task.result()
                    pointer_task = None
                    yield _format_event(
                        "song-change",
                        _song_change_payload(current, server_time=datetime.now(timezone.utc)),
                    )
                if user_task in done:
                    message = user_task.result()
                    user_task = None
                    frame = _format_user_event(message)
                    if frame is not None:
                        yield frame
                if depth_task in done:
                    depth = depth_task.result()
                    depth_task = None
                    yield _format_event("queue-depth", {"queue_depth": depth})
        finally:
            for task in (pointer_task, user_task, depth_task):
                if task is not None and not task.done():
                    task.cancel()
            broadcaster.unregister(queue)
            user_broadcaster.unregister(identity.user_id, user_queue)
            depth_broadcaster.unregister(depth_queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Defensive against a reverse proxy buffering the stream in front of the
            # app -- harmless no-op if there is none.
            "X-Accel-Buffering": "no",
        },
    )
