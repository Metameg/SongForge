"""``GET /now-playing`` — the radio pointer plus ``server_time`` (issue #8 criterion #3).

Serves from a process-local pointer cache warmed from Redis (issue #9, design D3/D4,
criteria #2 + #4), falling back to Postgres (``songforge.radio.state.get_now_playing``)
only when Redis is down or empty — repeated requests never touch Postgres once the
cache/Redis is warm. When the station is idle (no pointer yet, or no resolvable song)
responds 503 with ``{"status": "idle"}`` rather than fabricating a playing state.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, cast

from fastapi import APIRouter, Depends, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.db import get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.radio.pointer_cache import PgLoader, PointerCache, get_now_playing_cached
from songforge.radio.state import get_now_playing
from songforge.redis_client import get_redis

router = APIRouter(tags=["radio"])
log = get_logger(__name__)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


def get_redis_dependency() -> Redis:
    """The shared Redis client; overridden in tests to inject a fake/failing double."""
    return get_redis()


async def get_pg_loader_dependency() -> PgLoader:
    """The Postgres fallback loader.

    A separate dependency (rather than importing ``get_now_playing`` straight into the
    route) so HTTP-edge tests can inject a call-counting spy and directly observe the
    criterion #4 contract: "no Postgres read" once the local cache / Redis is warm.
    """
    return get_now_playing


def get_pointer_cache(request: Request) -> PointerCache:
    """This app instance's process-local pointer cache (issue #9, design D3).

    Set once per ``create_app()`` call on ``app.state.pointer_cache`` so each app
    instance — and so each test's ``TestClient`` — gets its own cache, avoiding
    cross-test/cross-instance TTL leakage.
    """
    return cast(PointerCache, request.app.state.pointer_cache)


@router.get("/now-playing")
async def now_playing(
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis_dependency),
    pg_loader: PgLoader = Depends(get_pg_loader_dependency),
    cache: PointerCache = Depends(get_pointer_cache),
) -> dict[str, Any]:
    """Return the current pointer, or a clear not-playing signal when idle."""
    record = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=session,
        pg_loader=pg_loader,
        redis_key=get_settings().radio_pointer_redis_key,
    )
    if record is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "idle"}
    return record.to_response(server_time=datetime.now(timezone.utc))
