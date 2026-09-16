"""Async SQLAlchemy engine/session plumbing.

Postgres is the source of truth (spec: users, songs, jobs, the ``radio_state`` pointer,
the authoritative queue). The engine is created lazily and cached per process; the web
tier connects through PgBouncer while the worker uses a direct connection for its
advisory lock and ``LISTEN/NOTIFY`` (spec #74) — both share this module, differing only
by the configured URL.
"""

from __future__ import annotations

import functools

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from songforge.config import Settings, get_settings


@functools.lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings: Settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        future=True,
    )


@functools.lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def check_database() -> bool:
    """Return True if a trivial query succeeds; used by the readiness probe."""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
