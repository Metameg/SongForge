"""End-to-end proof of issue #13's four acceptance criteria against the REAL
simulator (issue #11) and real Postgres, not hand-rolled fakes standing in for
either -- mirrors the "compose the 429 path against the real simulator end to end"
precedent (`tests/test_jobs_queue_integration.py`'s last test).

Flow per test: drive the real simulator's `POST /api/public/v1/MusicAI` to mint real
handles, insert a WAITING_FOR_WEBHOOK `Job` row with those handles (standing in for
what issue #12's dispatch would have already done), let the simulator's webhook
delivery land on OUR real `/api/generation/webhook` route (via `httpx.ASGITransport`
-- no real sockets), then run `claim_next_ingest_job` + `ingest_claimed_job` for real
against the real simulator's `/byId` and `/audio/{token}` routes and a moto S3
bucket.

Skips (not fails) if Postgres is unreachable or moto isn't installed -- same
convention as `tests/test_jobs_queue_integration.py` / `tests/test_storage.py`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")

_HAS_MOTO = importlib.util.find_spec("moto") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed"),
]

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")
_CONNECT_TIMEOUT_SECONDS = 1.5
_TEST_JOB_PREFIX = "test-issue13-e2e-"


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


def _settings() -> Any:
    from songforge.config import Settings

    return Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "https://s3.us-east-1.amazonaws.com",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "songforge-e2e-test",
            "S3_REGION": "us-east-1",
        }
    )


@pytest.fixture()
async def _clean_rows() -> Any:
    conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        await conn.execute("DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%")
        yield
        await conn.execute("DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%")
        # Songs are keyed by the simulator's real (random) conversion_id_1 -- no
        # stable prefix to filter on, so tests that created one delete it by id.
    finally:
        await conn.close()


async def _insert_waiting_job(
    sessionmaker: Any,
    job_id: str,
    *,
    task_id: str,
    conversion_id_1: str,
    conversion_id_2: str,
) -> None:
    from songforge.models import JOB_STATE_WAITING_FOR_WEBHOOK, Job

    async with sessionmaker() as session:
        session.add(
            Job(
                job_id=job_id, user_id="u1", prompt="a test prompt",
                state=JOB_STATE_WAITING_FOR_WEBHOOK, task_id=task_id,
                conversion_id_1=conversion_id_1, conversion_id_2=conversion_id_2,
            )
        )
        await session.commit()


async def test_webhook_then_ingest_happy_path_against_real_simulator(
    _clean_rows: None,
) -> None:
    """Acceptance criteria #1-#3 end to end: webhook records metadata + flips to
    INGEST_PENDING + NOTIFYs; ingest downloads + uploads to R2 + sets READY with an
    ordinary playable Song row."""
    from moto import mock_aws
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.jobs.generation_client import HttpGenerationClient
    from songforge.jobs.ingest import (
        HttpAudioDownloader,
        claim_next_ingest_job,
        ingest_claimed_job,
    )
    from songforge.models import JOB_STATE_INGEST_PENDING, JOB_STATE_READY, Job, Song
    from songforge.simulator.app import create_app as create_sim_app
    from songforge.simulator.app import wait_for_pending_webhooks
    from songforge.storage import ObjectStorage, audio_key
    from songforge.web.app import create_app as create_web_app
    from songforge.web.routes import webhook as webhook_module
    from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, make_sim_client

    settings = _settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session() -> Any:
        async with sessionmaker() as session:
            yield session

    web_app = create_web_app(settings)
    web_app.dependency_overrides[webhook_module.get_session] = _get_session
    webhook_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web.test"
    )

    sim_app = create_sim_app(http_client=webhook_client)
    sim_client = make_sim_client(sim_app)

    with mock_aws():
        storage = ObjectStorage.from_settings(settings)
        storage.ensure_bucket()

        handles: dict[str, Any] = {}
        try:
            async with sim_client:
                create_resp = await sim_client.post(
                    CREATE_PATH,
                    json={
                        **DEFAULT_BODY,
                        "webhook_url": "http://web.test/api/generation/webhook",
                    },
                )
                handles = create_resp.json()
                job_id = f"{_TEST_JOB_PREFIX}happy"
                await _insert_waiting_job(
                    sessionmaker, job_id, task_id=handles["task_id"],
                    conversion_id_1=handles["conversion_id_1"],
                    conversion_id_2=handles["conversion_id_2"],
                )

                await wait_for_pending_webhooks(sim_app)

            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                assert job.state == JOB_STATE_INGEST_PENDING
                assert job.audio_url is not None

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sim_app), base_url="http://simulator:8080"
            ) as sim_io_client:
                downloader = HttpAudioDownloader(
                    sim_io_client,
                    timeout=settings.ingest_download_timeout_seconds,
                    max_bytes=settings.ingest_max_download_bytes,
                )
                generation_client = HttpGenerationClient(settings, sim_io_client)

                async with sessionmaker() as session:
                    claimed = await claim_next_ingest_job(session)
                    assert claimed is not None
                    assert claimed.job_id == job_id
                    await ingest_claimed_job(
                        claimed, session=session, storage=storage,
                        downloader=downloader, generation_client=generation_client,
                        settings=settings,
                    )
                    await session.commit()

            async with sessionmaker() as session:
                final = await session.get(Job, job_id)
                assert final is not None
                assert final.state == JOB_STATE_READY
                assert final.song_id == handles["conversion_id_1"]

                song = await session.get(Song, handles["conversion_id_1"])
                assert song is not None
                assert song.object_key == audio_key(handles["conversion_id_1"])
                assert storage.exists(song.object_key)
                assert len(storage.download(song.object_key)) > 0
        finally:
            if handles:
                async with sessionmaker() as session:
                    # Delete the referencing `Job` row FIRST (now that the FK
                    # flush-ordering fix means `job.song_id` actually persists) --
                    # `fk_jobs_song_id_songs` forbids deleting the `Song` row while
                    # a job still points at it. `_clean_rows`'s own teardown also
                    # deletes this job by prefix, but that runs AFTER this block.
                    job_row = await session.get(Job, job_id)
                    if job_row is not None:
                        await session.delete(job_row)
                        await session.commit()
                    song = await session.get(Song, handles["conversion_id_1"])
                    if song is not None:
                        await session.delete(song)
                        await session.commit()
            await engine.dispose()
            await webhook_client.aclose()


async def test_expired_hint_url_is_refreshed_via_by_id_against_real_simulator(
    _clean_rows: None,
) -> None:
    """Acceptance criterion #4 end to end: the simulator's URL_EXPIRES_BEFORE_INGEST
    fault delivers an already-expired `conversion_path`; ingest's first download
    attempt gets a real 403 from `/audio/{token}`, refreshes via the real `/byId`,
    and the retried download succeeds."""
    from moto import mock_aws
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.jobs.generation_client import HttpGenerationClient
    from songforge.jobs.ingest import (
        HttpAudioDownloader,
        claim_next_ingest_job,
        ingest_claimed_job,
    )
    from songforge.models import JOB_STATE_READY, Job, Song
    from songforge.simulator.app import create_app as create_sim_app
    from songforge.simulator.app import wait_for_pending_webhooks
    from songforge.simulator.faults import FAULT_HEADER, Fault
    from songforge.storage import ObjectStorage
    from songforge.web.app import create_app as create_web_app
    from songforge.web.routes import webhook as webhook_module
    from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY

    settings = _settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session() -> Any:
        async with sessionmaker() as session:
            yield session

    web_app = create_web_app(settings)
    web_app.dependency_overrides[webhook_module.get_session] = _get_session
    webhook_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web.test"
    )

    sim_app = create_sim_app(http_client=webhook_client)
    sim_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test"
    )
    sim_client.headers[FAULT_HEADER] = Fault.URL_EXPIRES_BEFORE_INGEST.value

    with mock_aws():
        storage = ObjectStorage.from_settings(settings)
        storage.ensure_bucket()

        conversion_id_1: str | None = None
        try:
            async with sim_client:
                create_resp = await sim_client.post(
                    CREATE_PATH,
                    json={
                        **DEFAULT_BODY,
                        "webhook_url": "http://web.test/api/generation/webhook",
                    },
                )
                handles = create_resp.json()
                conversion_id_1 = handles["conversion_id_1"]
                job_id = f"{_TEST_JOB_PREFIX}expired"
                await _insert_waiting_job(
                    sessionmaker, job_id, task_id=handles["task_id"],
                    conversion_id_1=conversion_id_1,
                    conversion_id_2=handles["conversion_id_2"],
                )

                await wait_for_pending_webhooks(sim_app)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sim_app), base_url="http://simulator:8080"
            ) as sim_io_client:
                downloader = HttpAudioDownloader(
                    sim_io_client,
                    timeout=settings.ingest_download_timeout_seconds,
                    max_bytes=settings.ingest_max_download_bytes,
                )
                generation_client = HttpGenerationClient(settings, sim_io_client)

                async with sessionmaker() as session:
                    claimed = await claim_next_ingest_job(session)
                    assert claimed is not None
                    await ingest_claimed_job(
                        claimed, session=session, storage=storage,
                        downloader=downloader, generation_client=generation_client,
                        settings=settings,
                    )
                    await session.commit()

            async with sessionmaker() as session:
                final = await session.get(Job, job_id)
                assert final is not None
                assert final.state == JOB_STATE_READY  # recovered via by-id refresh
                assert final.song_id == conversion_id_1
        finally:
            if conversion_id_1 is not None:
                async with sessionmaker() as session:
                    # Delete the referencing `Job` row FIRST -- see the matching
                    # comment in the happy-path test's teardown above.
                    job_row = await session.get(Job, job_id)
                    if job_row is not None:
                        await session.delete(job_row)
                        await session.commit()
                    song = await session.get(Song, conversion_id_1)
                    if song is not None:
                        await session.delete(song)
                        await session.commit()
            await engine.dispose()
            await webhook_client.aclose()


async def test_duplicate_webhook_delivery_is_idempotent_against_real_simulator(
    _clean_rows: None,
) -> None:
    """PRD #6 idempotency end to end: the simulator's DUPLICATE_WEBHOOK fault fires
    the SAME webhook payload twice against our real route -- only the first delivery
    may transition/NOTIFY; the second must be a no-op."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.models import JOB_STATE_INGEST_PENDING, Job
    from songforge.simulator.app import create_app as create_sim_app
    from songforge.simulator.app import wait_for_pending_webhooks
    from songforge.simulator.faults import FAULT_HEADER, Fault
    from songforge.web.app import create_app as create_web_app
    from songforge.web.routes import webhook as webhook_module
    from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY

    settings = _settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_session() -> Any:
        async with sessionmaker() as session:
            yield session

    web_app = create_web_app(settings)
    web_app.dependency_overrides[webhook_module.get_session] = _get_session

    listener_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    received: list[str] = []

    def _on_notify(connection: object, pid: int, channel: str, payload: str) -> None:
        received.append(payload)

    try:
        await listener_conn.add_listener(settings.ingest_channel, _on_notify)

        webhook_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://web.test"
        )
        sim_app = create_sim_app(http_client=webhook_client)
        sim_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test"
        )
        sim_client.headers[FAULT_HEADER] = Fault.DUPLICATE_WEBHOOK.value

        try:
            async with sim_client:
                create_resp = await sim_client.post(
                    CREATE_PATH,
                    json={
                        **DEFAULT_BODY,
                        "webhook_url": "http://web.test/api/generation/webhook",
                    },
                )
                handles = create_resp.json()
                job_id = f"{_TEST_JOB_PREFIX}dup"
                await _insert_waiting_job(
                    sessionmaker, job_id, task_id=handles["task_id"],
                    conversion_id_1=handles["conversion_id_1"],
                    conversion_id_2=handles["conversion_id_2"],
                )

                await wait_for_pending_webhooks(sim_app)
                # Give the (already-completed) NOTIFY a moment to arrive.
                await asyncio.sleep(0.2)

            async with sessionmaker() as session:
                job = await session.get(Job, job_id)
                assert job is not None
                assert job.state == JOB_STATE_INGEST_PENDING

            assert received == [job_id]  # the duplicate did NOT NOTIFY a second time
        finally:
            await webhook_client.aclose()
            await engine.dispose()
    finally:
        await listener_conn.remove_listener(settings.ingest_channel, _on_notify)
        await listener_conn.close()
