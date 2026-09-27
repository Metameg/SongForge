"""Async SQLAlchemy engine/session plumbing.

Postgres is the source of truth (spec: users, songs, jobs, the ``radio_state`` pointer,
the authoritative queue). The engine is created lazily and cached per process; the web
tier connects through PgBouncer while the worker uses a direct connection for its
advisory lock and ``LISTEN/NOTIFY`` (spec #74).

``get_engine()`` is the shared, pooled engine used everywhere else. The radio leader's
advisory-lock connection is deliberately NOT this engine — see ``get_worker_lock_engine``
below (issue #17, PRD #74): a transaction pooler silently handing that connection to a
different backend session would silently break leadership, so it gets its own dedicated,
unpooled engine with a tuned ``tcp_user_timeout``.
"""

from __future__ import annotations

import functools
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from songforge.config import Settings, get_settings


@functools.lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings: Settings = get_settings()
    # Issue #19 (PRD #6 AC#2): only when the web tier sits behind PgBouncer in
    # TRANSACTION pooling mode do we disable asyncpg's server-side prepared-statement
    # cache -- under that pooling mode a logical connection can be handed a different
    # backend session between queries, so a statement prepared against one backend can
    # silently break on the next. Single-node/dev/tests (no pooler) keep the cache on
    # (today's construction, unchanged) since disabling it there is a needless perf hit.
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "future": True}
    if settings.db_pgbouncer_transaction_mode:
        kwargs["connect_args"] = {"statement_cache_size": 0}
    return create_async_engine(settings.database_url, **kwargs)


@functools.lru_cache(maxsize=1)
def get_worker_lock_engine() -> AsyncEngine:
    """Dedicated, unpooled engine for the radio leader's advisory-lock connection.

    PRD #74: the lock connection must be direct, never handed off by a transaction
    pooler mid-lifetime, so it uses ``NullPool`` instead of the shared pooled engine's
    default pool — one real connection per checkout, held for exactly as long as the
    caller holds it open.

    Also threads ``settings.worker_lock_tcp_user_timeout_seconds`` through to asyncpg's
    ``server_settings`` as the Postgres ``tcp_user_timeout`` GUC (milliseconds, as a
    string — the wire format libpq/asyncpg expect for GUC values): this is the failover
    mechanism (PRD: "tune TCP keepalive / tcp_user_timeout to ~10-15s") — it tells
    Postgres how fast to decide the leader's session is gone and tear its backend (and
    thus its advisory lock) down, so a blocked waiter can take over promptly.

    Issue #19 (PRD #6 AC#2): built from ``settings.effective_worker_database_url``, not
    ``settings.database_url`` — when ``WORKER_DATABASE_URL`` is set (pointing directly
    at Postgres while ``database_url`` points at PgBouncer), the lock connection must
    use that direct URL, or the very pooler this engine exists to bypass would end up
    holding it.
    """
    settings: Settings = get_settings()
    timeout_ms = int(settings.worker_lock_tcp_user_timeout_seconds * 1000)
    return create_async_engine(
        settings.effective_worker_database_url,
        poolclass=NullPool,
        future=True,
        connect_args={"server_settings": {"tcp_user_timeout": str(timeout_ms)}},
    )


def worker_asyncpg_dsn(settings: Settings) -> str:
    """Raw ``asyncpg.connect()`` DSN for the worker's direct Postgres connections.

    Issue #19 (PRD #6 AC#2): single source of truth for the DSN the worker's four
    LISTEN/NOTIFY modules (``worker/dispatch.py``, ``worker/watchdog.py``,
    ``worker/radio_coordinator.py``, ``worker/ingest.py``) and the advisory-lock engine
    above all need — ``settings.effective_worker_database_url`` (the direct/unpooled
    URL, falling back to ``database_url`` when no worker-specific URL is configured)
    with the ``+asyncpg`` SQLAlchemy driver tag stripped, since ``asyncpg.connect()``
    wants a plain ``postgresql://`` DSN and doesn't understand the driver suffix.
    """
    return settings.effective_worker_database_url.replace("+asyncpg", "")


@functools.lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def check_database() -> bool:
    """Return True if a trivial query succeeds; used by the readiness probe."""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
