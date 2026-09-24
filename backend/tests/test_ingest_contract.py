"""Data-contract audit (issue #13, phase 4): proves `jobs/ingest.ingest_claimed_job`'s
download + by-id-refresh path actually works against the REAL simulator HTTP surface
(`GET /audio/{token}` + `GET /byId`), not hand-authored `Downloader`/`GenerationClient`
fakes standing in for them.

`tests/test_ingest.py` exercises `ingest_claimed_job`'s decision logic with file-local
fakes (`_FakeDownloader`, `_FakeGenerationClient`) whose behavior -- what a download
failure looks like, what a refreshed URL looks like -- is entirely invented by the test
author. That's the right tool for testing the *decision* logic in isolation, but it can't
catch a real mismatch between what `HttpAudioDownloader`/`HttpGenerationClient` actually
do against a real HTTP response and what the fakes assume (e.g. the exact status code an
expired token returns, or whether a freshly-refreshed `/byId` URL is actually
downloadable). `tests/test_webhook_ingest_e2e_integration.py` already proves this end to
end, but only under `@pytest.mark.integration` (skipped without live Postgres + moto) --
so on a plain `pytest -q -m "not integration"` run, NOTHING exercises the real
downloader/by-id-refresh path. This file closes that gap with an in-memory SQLite
session and a plain in-process fake `ObjectStorage` sink (storage's own contract is
`tests/test_storage.py`'s job, not this one's) -- everything upstream of the sink
(simulator HTTP responses, `HttpAudioDownloader`, `HttpGenerationClient`,
`ingest_claimed_job`) is real production code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.generation_client import HttpGenerationClient
from songforge.jobs.ingest import HttpAudioDownloader, ingest_claimed_job
from songforge.models import JOB_STATE_INGEST_PENDING, JOB_STATE_READY, Base, Job, Song
from songforge.simulator.app import create_app as create_sim_app
from songforge.simulator.app import wait_for_pending_webhooks
from songforge.simulator.faults import FAULT_HEADER, Fault
from songforge.storage import audio_key
from tests.simulator_helpers import (
    CREATE_PATH,
    DEFAULT_BODY,
    WebhookReceiver,
    make_recording_sleep,
    make_webhook_client,
)


class _FakeStorage:
    """In-process sink standing in for R2/MinIO -- `ObjectStorage`'s own real-backend
    contract is `tests/test_storage.py`'s job (moto-backed); this file's contract is
    the download/by-id side, not the upload side."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def exists(self, key: str) -> bool:
        return key in self._objects

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        self._objects[key] = data
        return f"https://cdn.test/{key}"


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _settings() -> Settings:
    return Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=10)


async def _mint_completed_task(
    *, fault: Fault = Fault.NONE
) -> tuple[dict[str, Any], httpx.AsyncClient]:
    """Drives the real `POST /api/public/v1/MusicAI` + real webhook delivery to get a
    real, COMPLETED task on the real simulator app; returns the create handles and an
    httpx client wired (via `ASGITransport`) to that same simulator app for the
    downloader/by-id calls that follow."""
    receiver = WebhookReceiver()
    sleep_fn, _delay_calls = make_recording_sleep()
    sim_app = create_sim_app(http_client=make_webhook_client(receiver), sleep_fn=sleep_fn)
    sim_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test"
    )
    if fault is not Fault.NONE:
        sim_client.headers[FAULT_HEADER] = fault.value

    async with sim_client:
        create_resp = await sim_client.post(CREATE_PATH, json=DEFAULT_BODY)
        assert create_resp.status_code == 200
        handles: dict[str, Any] = create_resp.json()
        await wait_for_pending_webhooks(sim_app)

    assert len(receiver.received) == 1
    handles["_delivered_conversion_path"] = receiver.received[0]["conversion_path"]

    sim_io_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sim_app), base_url="http://sim.test"
    )
    return handles, sim_io_client


def _claimed_job_from_handles(job_id: str, handles: dict[str, Any]) -> Job:
    return Job(
        job_id=job_id,
        seq=1,
        user_id="user-1",
        prompt=str(DEFAULT_BODY["prompt"]),
        state=JOB_STATE_INGEST_PENDING,
        available_at=datetime.now(timezone.utc),
        task_id=handles["task_id"],
        conversion_id_1=handles["conversion_id_1"],
        conversion_id_2=handles["conversion_id_2"],
        audio_url=handles["_delivered_conversion_path"],
        audio_duration=120.0,
        title="Contract Test Song",
    )


# ── Happy path: real download from the real `/audio/{token}` route ───────────────


async def test_real_download_from_the_simulator_reaches_ready(
    session: AsyncSession,
) -> None:
    handles, sim_io_client = await _mint_completed_task()
    async with sim_io_client:
        settings = _settings()
        job = _claimed_job_from_handles("job-ingest-contract-happy", handles)
        storage = _FakeStorage()
        downloader = HttpAudioDownloader(
            sim_io_client,
            timeout=settings.ingest_download_timeout_seconds,
            max_bytes=settings.ingest_max_download_bytes,
        )
        generation_client = HttpGenerationClient(settings, sim_io_client)

        await ingest_claimed_job(
            job,
            session=session,
            storage=storage,  # type: ignore[arg-type]
            generation_client=generation_client,
            downloader=downloader,
            settings=settings,
        )

    assert job.state == JOB_STATE_READY
    key = audio_key(job.conversion_id_1)
    assert storage.exists(key)
    assert len(storage._objects[key]) > 0  # real bytes from the simulator's audio pool

    song = await session.get(Song, job.conversion_id_1)
    assert song is not None
    assert song.object_key == key


# ── Expired hint URL: real 403 from `/audio/{token}`, real refresh via `/byId` ────


async def test_real_expired_token_403_triggers_real_by_id_refresh_and_succeeds(
    session: AsyncSession,
) -> None:
    """`Fault.URL_EXPIRES_BEFORE_INGEST` makes the real simulator deliver a webhook
    whose `conversion_path` points at an already-expired token -- the real
    `/audio/{token}` route responds 403 (not a hand-crafted `AudioDownloadError`), so
    this proves `HttpAudioDownloader.download` actually maps that real 403 into the
    retry path, and that the real `/byId`-refreshed URL is actually downloadable."""
    handles, sim_io_client = await _mint_completed_task(
        fault=Fault.URL_EXPIRES_BEFORE_INGEST
    )
    async with sim_io_client:
        settings = _settings()
        job = _claimed_job_from_handles("job-ingest-contract-expired", handles)
        expired_url = job.audio_url
        storage = _FakeStorage()
        downloader = HttpAudioDownloader(
            sim_io_client,
            timeout=settings.ingest_download_timeout_seconds,
            max_bytes=settings.ingest_max_download_bytes,
        )
        generation_client = HttpGenerationClient(settings, sim_io_client)

        await ingest_claimed_job(
            job,
            session=session,
            storage=storage,  # type: ignore[arg-type]
            generation_client=generation_client,
            downloader=downloader,
            settings=settings,
        )

    assert job.state == JOB_STATE_READY
    assert job.audio_url != expired_url  # replaced by the real /byId-refreshed URL
    key = audio_key(job.conversion_id_1)
    assert storage.exists(key)
