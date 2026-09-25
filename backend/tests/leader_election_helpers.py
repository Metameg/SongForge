"""Shared scaffolding for the issue #17 leader-election / failover integration tests.

Split out of ``test_leader_election_integration.py`` (which grew past the repo's
500-line file convention) so the lock-semantics/CAS/catch-up cases and the
process-lifecycle failover + connection-tuning cases live in two focused files while
sharing one copy of the real-Postgres scaffolding:

- Postgres-reachability probe + autouse skip (a plain TCP probe, so a dead host is one
  fast skip, not a per-test connect timeout).
- Autouse ``alembic upgrade head`` against the target DB, so tests see the real
  ``songs``/``radio_state``/``playback_queue`` schema (landed by #8/#14).
- ``conn`` — a raw ``asyncpg`` connection with this suite's own rows (and the shared
  ``radio_state`` singleton) cleaned before and after each test.
- ``pg_sessionmaker`` — a real SQLAlchemy async sessionmaker the REAL
  ``worker.radio_coordinator._tick`` is monkeypatched onto (via ``_run_tick``), so the
  actual deliverable code is exercised, not a reimplementation.

Fixtures defined here are imported into each test module's namespace (a standard pytest
pattern); the autouse ones then apply per importing module. This module contains no
``test_`` functions, so pytest never collects it as a test file itself.

Connects to ``TEST_DATABASE_URL`` if set, else the docker-compose dev default (see
``test_jobs_queue_integration.py``). Deliberately NOT the process env ``DATABASE_URL``
(``tests/conftest.py`` points that at a closed port so the fast unit suite never touches
a live datastore).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

_BACKEND_DIR = Path(__file__).resolve().parent.parent

_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")

_CONNECT_TIMEOUT_SECONDS = 1.5
_TEST_SONG_PREFIX = "test-issue17-"

# Test-only advisory-lock keys, well clear of `settings.radio_advisory_lock_key`
# (927341) so these tests never contend with a real worker that might be running
# against the same dev Postgres. Each test that needs a lock takes its own offset.
_LOCK_KEY = 5_170_017_000


def _postgres_reachable() -> bool:
    parts = urlsplit(_ASYNCPG_DSN)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_postgres() -> None:
    """Skip the importing module up front if Postgres is unreachable -- a plain TCP
    probe so every test doesn't pay its own connect-timeout on a dead host."""
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
    """Apply real Alembic migrations before any test in the importing module runs, so
    these tests exercise the actual `songs`/`radio_state`/`playback_queue` tables
    (already landed by issue #8/#14)."""
    env = dict(os.environ)
    env["DATABASE_URL"] = _SETTINGS_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the integration Postgres:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.fixture()
async def conn() -> AsyncIterator["asyncpg.Connection[asyncpg.Record]"]:
    """A connection to the real target DB, with this suite's own test rows cleaned up
    before and after each test. `radio_state` is a real singleton row (id=1) shared
    across the whole schema, so it MUST be cleared between tests too, not just this
    suite's own prefixed rows."""
    connection = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)

    async def _clean() -> None:
        await connection.execute("DELETE FROM playback_queue")
        await connection.execute("DELETE FROM radio_state WHERE id = 1")
        await connection.execute(
            "DELETE FROM songs WHERE id LIKE $1", f"{_TEST_SONG_PREFIX}%"
        )

    await _clean()
    try:
        yield connection
    finally:
        await _clean()
        await connection.close()


@pytest.fixture()
async def pg_sessionmaker():  # type: ignore[no-untyped-def]
    """A real SQLAlchemy async engine/sessionmaker against the integration Postgres --
    what `songforge.worker.radio_coordinator._tick` is monkeypatched to use instead of
    the process-wide (closed-port, per `tests/conftest.py`) cached one."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_SETTINGS_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _make_settings():  # type: ignore[no-untyped-def]
    from songforge.config import Settings

    return Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )


async def _insert_static_song(
    connection: "asyncpg.Connection[asyncpg.Record]",
    song_id: str,
    *,
    duration_seconds: int = 180,
) -> None:
    full_id = f"{_TEST_SONG_PREFIX}{song_id}"
    await connection.execute(
        "INSERT INTO songs (id, title, source, object_key, duration_seconds) "
        "VALUES ($1, $1, 'static', $2, $3)",
        full_id,
        f"audio/{full_id}.mp3",
        duration_seconds,
    )


async def _run_tick(
    monkeypatch: pytest.MonkeyPatch,
    settings,  # type: ignore[no-untyped-def]
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    history,  # type: ignore[no-untyped-def]
    redis,  # type: ignore[no-untyped-def]
) -> float:
    """Drives the REAL `worker.radio_coordinator._tick` against real Postgres, by
    monkeypatching its module-level `get_sessionmaker` (used at call time) rather than
    the process-wide `functools.lru_cache`d one -- keeps this test isolated from any
    other test module's cached (closed-port) engine."""
    from songforge.worker import radio_coordinator as worker_coordinator

    monkeypatch.setattr(worker_coordinator, "get_sessionmaker", lambda: pg_sessionmaker)
    return await worker_coordinator._tick(settings, history, redis)
