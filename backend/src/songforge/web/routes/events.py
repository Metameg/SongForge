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


def _format_event(event: str, payload: dict[str, Any]) -> str:
    """Hand-build one `text/event-stream` frame: a named event + a JSON data line."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@router.get("/events")
async def events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis_dependency),
    pg_loader: PgLoader = Depends(get_pg_loader_dependency),
    cache: PointerCache = Depends(get_pointer_cache),
    broadcaster: PointerBroadcaster = Depends(get_pointer_broadcaster),
    user_broadcaster: UserEventBroadcaster = Depends(get_user_event_broadcaster),
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

    async def stream() -> AsyncIterator[str]:
        nonlocal current
        pointer_task: asyncio.Task[PointerRecord] | None = None
        user_task: asyncio.Task[dict[str, Any]] | None = None
        try:
            if current is None:
                yield _format_event("idle", {"status": "idle"})
            else:
                yield _format_event(
                    "song-change", current.to_response(server_time=datetime.now(timezone.utc))
                )

            while True:
                if await request.is_disconnected():
                    break
                if pointer_task is None:
                    pointer_task = asyncio.ensure_future(queue.get())
                if user_task is None:
                    user_task = asyncio.ensure_future(user_queue.get())

                done, _pending = await asyncio.wait(
                    {pointer_task, user_task},
                    timeout=heartbeat_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if not done:
                    # Heartbeat: re-emit the last-known pointer, held locally -- this
                    # is the one place a naive implementation would silently
                    # reintroduce per-listener datastore load on every heartbeat.
                    # Never re-sends a job-failed frame (one-shot, no replay).
                    if current is not None:
                        yield _format_event(
                            "song-change",
                            current.to_response(server_time=datetime.now(timezone.utc)),
                        )
                    continue

                if pointer_task in done:
                    current = pointer_task.result()
                    pointer_task = None
                    yield _format_event(
                        "song-change",
                        current.to_response(server_time=datetime.now(timezone.utc)),
                    )
                if user_task in done:
                    message = user_task.result()
                    user_task = None
                    yield _format_event("job-failed", {"job_id": message.get("job_id")})
        finally:
            for task in (pointer_task, user_task):
                if task is not None and not task.done():
                    task.cancel()
            broadcaster.unregister(queue)
            user_broadcaster.unregister(identity.user_id, user_queue)

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
