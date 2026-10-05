"""``GET /queue`` -- waiting-queue depth + the caller's own positions (issue #38).

STUB (TDD red): the router has no endpoint yet, so ``GET /queue`` 404s. The implementer
adds the handler (read-only identity -- never sets a cookie) and registers the router in
``songforge.web.app.create_app``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.db import get_sessionmaker

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
