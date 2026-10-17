"""`POST /create` end to end against real Postgres (issue #12, criterion #1):
identity cookie minted + signed, job persisted as QUEUED, and the new-job channel
genuinely NOTIFYed -- proving the default `_pg_notify` dependency (not the spy
`tests/test_create_route.py` substitutes) actually delivers.

Drives the app via `httpx.ASGITransport` (a genuinely async transport that shares the
test's own event loop) rather than the sync `fastapi.testclient.TestClient`, which runs
requests through its own background-thread event loop -- fine for the fast SQLite edge
tests, but asyncpg connections are strictly bound to the loop that created them, so a
real Postgres session built in the test's loop can't be used from that other thread.

Connects to `TEST_DATABASE_URL` if set, else the docker-compose dev default (see
`tests/test_jobs_queue_integration.py`). Skips (not fails) if Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")
_CONNECT_TIMEOUT_SECONDS = 1.5


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
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
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


def _settings():  # type: ignore[no-untyped-def]
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


@pytest.fixture()
async def _clean_jobs():  # type: ignore[no-untyped-def]
    """`POST /create` mints its own job_id (a plain UUID4 hex, not prefixed), so this
    can't filter by a test-owned prefix like `tests/test_jobs_queue_integration.py`
    does -- truncate the whole table instead. Safe: this integration Postgres is an
    ephemeral, throwaway instance dedicated to this worktree's tests."""
    conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        # CASCADE (issue #14): `playback_queue.job_id` now FKs `jobs.job_id`, so a
        # plain `TRUNCATE jobs` is rejected once any queue row exists. CASCADE only
        # follows that one incoming FK -- nothing else references `jobs` -- so this
        # truncates `playback_queue` alongside it, never `songs`/`radio_state`.
        await conn.execute("TRUNCATE TABLE jobs CASCADE")
        yield
        await conn.execute("TRUNCATE TABLE jobs CASCADE")
    finally:
        await conn.close()


async def test_create_persists_a_queued_job_and_mints_an_identity_cookie(
    _clean_jobs: None,
) -> None:
    import httpx
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.web.app import create_app
    from songforge.web.identity import unsign
    from songforge.web.routes import create as create_module

    settings = _settings()
    app = create_app(settings)
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session():  # type: ignore[no-untyped-def]
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[create_module.get_session] = _get_session

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/create", json={"prompt": "a synthy hymn"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "QUEUED"

        cookie_raw = resp.cookies.get(settings.identity_cookie_name)
        assert cookie_raw is not None
        identity = unsign(cookie_raw, secret=settings.session_secret)
        assert identity is not None

        async with sessionmaker() as session:
            row = (
                await session.execute(
                    text("SELECT user_id, state FROM jobs WHERE job_id = :job_id"),
                    {"job_id": body["job_id"]},
                )
            ).one()
        assert row.user_id == identity
        assert row.state == "QUEUED"
    finally:
        await engine.dispose()


async def test_create_notifies_the_new_job_channel_for_real(_clean_jobs: None) -> None:
    """Proves the *default* `_pg_notify` dependency (not a test spy) delivers a real
    LISTEN/NOTIFY -- the fast `tests/test_create_route.py` variant only proves ordering
    against a spy, never that a real Postgres NOTIFY is actually sent and committed."""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.web.app import create_app
    from songforge.web.routes import create as create_module

    settings = _settings()
    app = create_app(settings)
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session():  # type: ignore[no-untyped-def]
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[create_module.get_session] = _get_session

    listener_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    notifications: list[str] = []
    woke = asyncio.Event()

    def _on_notify(connection: object, pid: int, channel: str, payload: str) -> None:
        notifications.append(payload)
        woke.set()

    try:
        await listener_conn.add_listener(settings.jobs_new_channel, _on_notify)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post("/create", json={"prompt": "a synthy hymn"})
            assert resp.status_code == 200
            try:
                await asyncio.wait_for(woke.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pytest.fail("listener did not wake within 5s of POST /create")
            assert notifications == [resp.json()["job_id"]]
        finally:
            await listener_conn.remove_listener(settings.jobs_new_channel, _on_notify)
    finally:
        await listener_conn.close()
        await engine.dispose()


async def test_concurrent_creates_from_the_same_identity_each_persist_a_distinct_job(
    _clean_jobs: None,
) -> None:
    """Multiple `POST /create` calls sharing one identity cookie (e.g. a user with two
    tabs open) must never collide -- each persists its own row with the real,
    Postgres-generated `seq` (an `Identity` column, criterion #2's FIFO ordering key),
    never overwriting or double-assigning another concurrent create's row. Uses real
    concurrency (`asyncio.gather`) against real Postgres, not sequential calls, so this
    exercises genuine concurrent `Identity` generation, not just distinct calls."""
    import httpx
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.models import Job
    from songforge.web.app import create_app
    from songforge.web.identity import mint, sign
    from songforge.web.routes import create as create_module

    settings = _settings()
    app = create_app(settings)
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session():  # type: ignore[no-untyped-def]
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[create_module.get_session] = _get_session

    user_id = mint()
    signed = sign(user_id, secret=settings.session_secret)

    try:
        transport = httpx.ASGITransport(app=app)

        async def _create() -> httpx.Response:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                c.cookies.set(settings.identity_cookie_name, signed)
                return await c.post("/create", json={"prompt": "a concurrent-create song"})

        responses = await asyncio.gather(*(_create() for _ in range(5)))

        assert all(r.status_code == 200 for r in responses)
        job_ids = [r.json()["job_id"] for r in responses]
        assert len(set(job_ids)) == 5  # every create persisted a distinct job

        async with sessionmaker() as session:
            rows = (
                await session.scalars(select(Job).where(Job.job_id.in_(job_ids)))
            ).all()
        assert len(rows) == 5
        assert all(row.user_id == user_id for row in rows)
        seqs = {row.seq for row in rows}
        assert len(seqs) == 5  # distinct, genuinely Postgres-generated `seq` per job
    finally:
        await engine.dispose()
