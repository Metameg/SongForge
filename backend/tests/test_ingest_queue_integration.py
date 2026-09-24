"""Postgres-only integration tests for the ingest claim query (issue #13). Mirrors
`tests/test_jobs_queue_integration.py`'s fixtures: skip fast if Postgres is
unreachable, otherwise `alembic upgrade head` so these tests see migration `0004`'s
DDL (the `ingest_attempts`/`song_id`/etc columns and the `ix_jobs_ingest_pending_seq`
partial index).

`FOR UPDATE SKIP LOCKED` claim ordering, the partial index, and `LISTEN`/`NOTIFY` are
Postgres-only behaviours SQLite cannot emulate (see `.orchestrator/CONTEXT.md`
"Testing"). Connects to `TEST_DATABASE_URL` if set, else the docker-compose dev
default -- deliberately NOT the process env `DATABASE_URL` (`tests/conftest.py`
points that at a closed port so the fast unit/edge tests never touch a live
datastore).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")
_CONNECT_TIMEOUT_SECONDS = 1.5
_TEST_JOB_PREFIX = "test-issue13-"


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
        cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the integration Postgres:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.fixture()
async def conn() -> AsyncIterator[Any]:
    connection = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)

    async def _clean() -> None:
        try:
            await connection.execute(
                "DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%"
            )
        except asyncpg.PostgresError:
            pass

    await _clean()
    try:
        yield connection
    finally:
        await _clean()
        await connection.close()


async def _insert_ingest_pending(connection: Any, job_id: str, conversion_id: str) -> None:
    await connection.execute(
        "INSERT INTO jobs (job_id, user_id, prompt, state, task_id, conversion_id_1, "
        "conversion_id_2, attempts, ingest_attempts) "
        "VALUES ($1, 'u1', 'a test prompt', 'INGEST_PENDING', $2, $2, $2, 0, 0)",
        f"{_TEST_JOB_PREFIX}{job_id}", conversion_id,
    )


_CLAIM_SQL = """
    SELECT job_id FROM jobs
    WHERE state = 'INGEST_PENDING' AND available_at <= now()
    ORDER BY seq
    FOR UPDATE SKIP LOCKED
    LIMIT 1
"""


async def test_claim_returns_jobs_in_fifo_seq_order(conn: Any) -> None:
    await _insert_ingest_pending(conn, "a", "conv-a")
    await _insert_ingest_pending(conn, "b", "conv-b")

    async with conn.transaction():
        first = await conn.fetchrow(_CLAIM_SQL)
        assert first is not None
        assert first["job_id"] == f"{_TEST_JOB_PREFIX}a"
        await conn.execute(
            "UPDATE jobs SET state = 'READY' WHERE job_id = $1", first["job_id"]
        )

    async with conn.transaction():
        second = await conn.fetchrow(_CLAIM_SQL)
        assert second is not None
        assert second["job_id"] == f"{_TEST_JOB_PREFIX}b"


async def test_skip_locked_never_double_claims_across_concurrent_connections(
    conn: Any,
) -> None:
    await _insert_ingest_pending(conn, "x", "conv-x")
    await _insert_ingest_pending(conn, "y", "conv-y")

    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        tx_a, tx_b = conn_a.transaction(), conn_b.transaction()
        await tx_a.start()
        await tx_b.start()
        try:
            claimed_a, claimed_b = await asyncio.gather(
                conn_a.fetchrow(_CLAIM_SQL), conn_b.fetchrow(_CLAIM_SQL)
            )
        except BaseException:
            await tx_a.rollback()
            await tx_b.rollback()
            raise
        else:
            await tx_a.commit()
            await tx_b.commit()

        assert claimed_a is not None
        assert claimed_b is not None
        assert claimed_a["job_id"] != claimed_b["job_id"]
    finally:
        await conn_a.close()
        await conn_b.close()


async def test_backoff_requeued_job_is_not_reclaimed_before_available_at(
    conn: Any,
) -> None:
    await conn.execute(
        "INSERT INTO jobs (job_id, user_id, prompt, state, task_id, conversion_id_1, "
        "conversion_id_2, attempts, ingest_attempts, available_at) "
        "VALUES ($1, 'u1', 'p', 'INGEST_PENDING', 't', 'c', 'c2', 0, 1, "
        "now() + interval '1 hour')",
        f"{_TEST_JOB_PREFIX}not-due-yet",
    )
    await _insert_ingest_pending(conn, "ready-now", "conv-ready")

    async with conn.transaction():
        first = await conn.fetchrow(_CLAIM_SQL)
        assert first is not None
        assert first["job_id"] == f"{_TEST_JOB_PREFIX}ready-now"
        await conn.execute(
            "UPDATE jobs SET state = 'READY' WHERE job_id = $1", first["job_id"]
        )

    async with conn.transaction():
        second = await conn.fetchrow(_CLAIM_SQL)
    assert second is None


async def test_partial_index_exists_on_seq_where_ingest_pending(conn: Any) -> None:
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'"
    )
    partial = [
        r for r in rows
        if "seq" in r["indexdef"].lower() and "where" in r["indexdef"].lower()
        and "ingest_pending" in r["indexdef"].lower()
    ]
    assert partial, (
        f"no partial index on jobs(seq) WHERE ...INGEST_PENDING found; "
        f"indexes were: {list(rows)}"
    )


async def test_listen_notify_wakes_a_waiting_listener() -> None:
    from songforge.config import Settings

    settings = Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )

    listener_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    received: list[str] = []
    woke = asyncio.Event()

    def _on_notify(connection: object, pid: int, channel: str, payload: str) -> None:
        received.append(payload)
        woke.set()

    try:
        await listener_conn.add_listener(settings.ingest_channel, _on_notify)
        notifier_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
        try:
            await notifier_conn.execute(
                "SELECT pg_notify($1, $2)", settings.ingest_channel,
                f"{_TEST_JOB_PREFIX}notify",
            )
            try:
                await asyncio.wait_for(woke.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pytest.fail("listener did not wake within 5s of pg_notify")
        finally:
            await notifier_conn.close()
    finally:
        await listener_conn.remove_listener(settings.ingest_channel, _on_notify)
        await listener_conn.close()

    assert received == [f"{_TEST_JOB_PREFIX}notify"]


@pytest.fixture()
async def sessionmaker() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_SETTINGS_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_claim_next_ingest_job_returns_the_oldest_ingest_pending_job(
    conn: Any, sessionmaker: Any
) -> None:
    from songforge.jobs.ingest import claim_next_ingest_job

    await _insert_ingest_pending(conn, "orm-a", "conv-orm-a")
    await _insert_ingest_pending(conn, "orm-b", "conv-orm-b")

    async with sessionmaker() as session:
        job = await claim_next_ingest_job(session)
        assert job is not None
        assert job.job_id == f"{_TEST_JOB_PREFIX}orm-a"
        assert job.state == "INGEST_PENDING"  # unchanged by claim (see decision #5)


async def test_claim_next_ingest_job_never_double_claims_across_concurrent_sessions(
    conn: Any, sessionmaker: Any
) -> None:
    from songforge.jobs.ingest import claim_next_ingest_job
    from songforge.models import JOB_STATE_READY

    await _insert_ingest_pending(conn, "orm-x", "conv-orm-x")
    await _insert_ingest_pending(conn, "orm-y", "conv-orm-y")

    async def _claim_and_finish() -> str | None:
        async with sessionmaker() as session:
            job = await claim_next_ingest_job(session)
            if job is None:
                return None
            job.state = JOB_STATE_READY  # simulate ingest_claimed_job's outcome
            await session.commit()
            return job.job_id

    results = await asyncio.gather(_claim_and_finish(), _claim_and_finish())
    claimed = {r for r in results if r is not None}
    assert claimed == {f"{_TEST_JOB_PREFIX}orm-x", f"{_TEST_JOB_PREFIX}orm-y"}


async def test_ingest_claimed_job_finalize_ready_against_real_postgres_fk(
    conn: Any, sessionmaker: Any
) -> None:
    """Regression test for the CRITICAL FK flush-ordering finding (prod-validation
    report): `_finalize_ready` must flush the staged `Song` INSERT before mutating
    `job.state`/`job.song_id`, or real Postgres's `fk_jobs_song_id_songs` constraint
    rejects the `Job` UPDATE (`Job.song_id` and `Song` have no declared
    `relationship()`, so SQLAlchemy's unit-of-work has no ordering information of its
    own). SQLite -- every other ingest test in this suite -- doesn't enforce foreign
    keys, so this bug was invisible everywhere except against a real database; this
    test runs `ingest_claimed_job` end to end (minus the network/S3 ports, which are
    fakes) against real Postgres specifically to catch it."""
    from songforge.config import Settings
    from songforge.jobs.ingest import ingest_claimed_job
    from songforge.models import JOB_STATE_READY, Job, Song

    job_id = f"{_TEST_JOB_PREFIX}fk-flush-order"
    conversion_id = "conv-fk-flush-order"
    await _insert_ingest_pending(conn, "fk-flush-order", conversion_id)

    class _FakeStorage:
        def exists(self, key: str) -> bool:
            return False

        def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
            return f"https://cdn.test/{key}"

    class _FakeDownloader:
        async def download(self, url: str) -> bytes:
            return b"fake-mp3-bytes"

    class _FakeGenerationClient:
        async def create(self, **kwargs: object) -> None:
            raise AssertionError("must not be called")

        async def get_audio_url_by_id(self, task_id: str) -> str:
            raise AssertionError("must not be called")

    settings = Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )

    try:
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            job.audio_url = "http://musicgpt.test/audio/hint-token"

            await ingest_claimed_job(
                job,
                session=session,
                storage=_FakeStorage(),  # type: ignore[arg-type]
                generation_client=_FakeGenerationClient(),  # type: ignore[arg-type]
                downloader=_FakeDownloader(),  # type: ignore[arg-type]
                settings=settings,
            )
            # Must NOT raise fk_jobs_song_id_songs -- this is the whole point of
            # the test: the Song INSERT must already be flushed before this commits
            # the Job UPDATE that references it.
            await session.commit()

        async with sessionmaker() as session:
            final = await session.get(Job, job_id)
            assert final is not None
            assert final.state == JOB_STATE_READY
            assert final.song_id == conversion_id

            song = await session.get(Song, conversion_id)
            assert song is not None
            assert song.object_key.endswith(f"{conversion_id}.mp3")
    finally:
        # Delete the `jobs` row FIRST -- it FK-references the `songs` row this test
        # creates, so the song can't be deleted while the job still points at it.
        # The `conn` fixture's own teardown also deletes `jobs` rows by prefix, but
        # that runs AFTER this block, and the `songs` row (keyed by `conversion_id`,
        # with no shared prefix to filter on) needs its own explicit cleanup here
        # (mirrors `test_webhook_ingest_e2e_integration.py`'s equivalent teardown).
        await conn.execute("DELETE FROM jobs WHERE job_id = $1", job_id)
        await conn.execute("DELETE FROM songs WHERE id = $1", conversion_id)
