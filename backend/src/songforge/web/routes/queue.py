"""``GET /queue`` -- waiting-queue depth + the caller's own positions (issue #38).

Thin wrapper over ``songforge.radio.queue.get_queue_status``. Like ``GET /events`` (and
unlike ``GET /quota``) this only READS identity: it never sets a cookie, so a cookieless
caller resolves to a throwaway identity with no jobs (empty ``positions``) while
``depth`` stays correct.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.db import get_sessionmaker
from songforge.radio.queue import get_queue_status
from songforge.web.identity import resolve_identity

router = APIRouter(tags=["queue"])


class QueuePosition(BaseModel):
    """One of the caller's queued jobs and its 1-based global position."""

    job_id: str
    position: int


class QueueResponse(BaseModel):
    """``GET /queue`` response."""

    depth: int
    positions: list[QueuePosition]


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


@router.get("/queue", response_model=QueueResponse)
async def get_queue(
    request: Request, session: AsyncSession = Depends(get_session)
) -> QueueResponse:
    """Report waiting depth and the caller's own queue positions (read-only identity)."""
    identity = resolve_identity(request, get_settings())
    status = await get_queue_status(session, identity.user_id)
    return QueueResponse(
        depth=status.depth,
        positions=[
            QueuePosition(job_id=e.job_id, position=e.position) for e in status.positions
        ],
    )
