"""``GET /now-playing`` — the radio pointer plus ``server_time`` (issue #8, criterion #3).

Reads truth from Postgres via ``songforge.radio.state.get_now_playing``; when the station
is idle (no pointer yet, or no resolvable song) responds 503 with ``{"status": "idle"}``
rather than fabricating a playing state.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.db import get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.radio.state import get_now_playing

router = APIRouter(tags=["radio"])
log = get_logger(__name__)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


@router.get("/now-playing")
async def now_playing(
    response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Return the current pointer, or a clear not-playing signal when idle."""
    view = await get_now_playing(session)
    if view is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "idle"}
    return view.to_response(server_time=datetime.now(timezone.utc))
