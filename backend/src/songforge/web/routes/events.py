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
    heartbeat_seconds: float = Depends(get_heartbeat_seconds),
) -> StreamingResponse:
    """Stream `song-change` events: the current pointer on connect, then every push."""
    current: PointerRecord | None = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=session,
        pg_loader=pg_loader,
        redis_key=get_settings().radio_pointer_redis_key,
    )
    queue = broadcaster.register()

    async def stream() -> AsyncIterator[str]:
        nonlocal current
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
                try:
                    current = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                except TimeoutError:
                    # Heartbeat: re-emit the last-known pointer, held locally -- this
                    # is the one place a naive implementation would silently
                    # reintroduce per-listener datastore load on every heartbeat.
                    if current is not None:
                        yield _format_event(
                            "song-change",
                            current.to_response(server_time=datetime.now(timezone.utc)),
                        )
                    continue

                yield _format_event(
                    "song-change", current.to_response(server_time=datetime.now(timezone.utc))
                )
        finally:
            broadcaster.unregister(queue)

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
