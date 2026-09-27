"""End-to-end correlation trace, part 2: ingest, webhook, and watchdog each re-bind a
job's submit-time correlation ID for their own log lines (issue #18, acceptance
criterion #1 / gap #1). See `tests/test_correlation_trace.py`'s module docstring for
the full rationale -- split out per this repo's "keep files under 500 lines" / split-
large-test-files convention (mirrors `test_leader_election_*`).

Production change that turns these green: `jobs/ingest.py::ingest_claimed_job`,
`web/routes/webhook.py::receive_webhook`, and each `jobs/watchdog.py::sweep_*` binding
`job.correlation_id` into the correlation contextvar for the duration of their work.
"""

from __future__ import annotations

import io
import itertools
import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge import correlation
from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GenerationStatus,
)
from songforge.jobs.ingest import ingest_claimed_job
from songforge.jobs.watchdog import sweep_waiting_overdue
from songforge.logging_setup import configure_logging
from songforge.models import (
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
    PlaybackQueue,
)
from songforge.storage import audio_key
from songforge.web.app import create_app
from songforge.web.routes.webhook import get_notify_dependency, get_semaphore_dependency
from songforge.web.routes.webhook import get_session as webhook_get_session

_ids = itertools.count(1)


def _assign_id(mapper: Any, connection: Any, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_ids)


@pytest.fixture(autouse=True)
def _sqlite_id_shim() -> Iterator[None]:
    """See `tests/test_ingest.py`'s matching shim: SQLite `create_all` doesn't
    auto-populate a `BigInteger` `Identity()` PK the way real Postgres does."""
    event.listen(PlaybackQueue, "before_insert", _assign_id)
    yield
    event.remove(PlaybackQueue, "before_insert", _assign_id)


def _log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    text = stream.getvalue().strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


# ── ingest re-binds the job's correlation ID (gap #1) ──────────────────────────────


class _FakeStorage:
    """Idempotent-re-ingest branch: the object already exists, so no download/upload
    happens -- keeps this test focused on the correlation bind, not the full ingest
    happy path (already covered by `tests/test_ingest.py`)."""

    def __init__(self, existing_key: str) -> None:
        self._existing_key = existing_key

    def exists(self, key: str) -> bool:
        return key == self._existing_key

    def put(self, key: str, data: bytes) -> str:
        raise AssertionError("must not upload when the object already exists")


class _UnusedDownloader:
    async def download(self, url: str) -> bytes:
        raise AssertionError("must not download on the idempotent-exists branch")


class _UnusedGenerationClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("not exercised on the idempotent-exists branch")

    async def get_status_by_id(self, task_id: str) -> None:
        raise AssertionError("not exercised on the idempotent-exists branch")


@pytest.fixture()
async def sqlite_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


async def test_ingest_claimed_job_binds_job_correlation_id_into_log_lines(
    sqlite_session: AsyncSession,
) -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    correlation.clear_correlation_id()
    key = audio_key("conv-1")
    job = Job(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="p",
        state="INGEST_PENDING",
        task_id="task-1",
        conversion_id_1="conv-1",
        audio_url="http://musicgpt.test/audio/token",
        audio_duration=180.0,
        title="A Song",
        ingest_attempts=0,
        available_at=datetime.now(timezone.utc),
        correlation_id="trace-ingest-1",
    )  # type: ignore[arg-type]
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=sqlite_session,
        storage=_FakeStorage(key),  # type: ignore[arg-type]
        generation_client=_UnusedGenerationClient(),  # type: ignore[arg-type]
        downloader=_UnusedDownloader(),  # type: ignore[arg-type]
        settings=settings,
    )

    completed_lines = [
        line
        for line in _log_lines(stream)
        if line.get("event") == "ingest_completed_idempotent"
    ]
    assert completed_lines, "expected an ingest_completed_idempotent log line"
    assert completed_lines[0]["correlation_id"] == "trace-ingest-1"


async def test_ingest_claimed_job_resets_correlation_id_after_processing(
    sqlite_session: AsyncSession,
) -> None:
    correlation.clear_correlation_id()
    key = audio_key("conv-2")
    job = Job(
        job_id="job-2",
        seq=2,
        user_id="user-1",
        prompt="p",
        state="INGEST_PENDING",
        task_id="task-2",
        conversion_id_1="conv-2",
        audio_url="http://musicgpt.test/audio/token-2",
        audio_duration=180.0,
        title="Another Song",
        ingest_attempts=0,
        available_at=datetime.now(timezone.utc),
        correlation_id="trace-ingest-2",
    )  # type: ignore[arg-type]
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=sqlite_session,
        storage=_FakeStorage(key),  # type: ignore[arg-type]
        generation_client=_UnusedGenerationClient(),  # type: ignore[arg-type]
        downloader=_UnusedDownloader(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert correlation.get_correlation_id() is None


# ── webhook re-binds the job's correlation ID (gap #1) ─────────────────────────────


@pytest.fixture()
async def webhook_sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _NotifySpy:
    async def __call__(self, channel: str, payload: str) -> None:
        return None


class _NoopSemaphore:
    async def release(self, user_id: str) -> None:
        return None


async def test_webhook_route_binds_job_correlation_id_into_log_lines(
    webhook_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with webhook_sessionmaker() as session:
        session.add(
            Job(
                job_id="job-webhook-1",
                seq=1,
                user_id="user-1",
                prompt="p",
                state=JOB_STATE_WAITING_FOR_WEBHOOK,
                task_id="task-webhook-1",
                conversion_id_1="conv-webhook-1",
                webhook_url="http://web:8000/api/generation/webhook",
                correlation_id="trace-webhook-1",
            )  # type: ignore[arg-type]
        )
        await session.commit()

    app = create_app()
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with webhook_sessionmaker() as session:
            yield session

    app.dependency_overrides[webhook_get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    app.dependency_overrides[get_semaphore_dependency] = lambda: _NoopSemaphore()
    client = TestClient(app)

    resp = client.post(
        "/api/generation/webhook",
        json={
            "task_id": "task-webhook-1",
            "conversion_id": "conv-webhook-1",
            "conversion_path": "http://musicgpt.test/audio/token",
            "conversion_duration": 180.0,
            "title": "A Traced Song",
        },
    )

    assert resp.status_code == 200
    ingest_pending_lines = [
        line for line in _log_lines(stream) if line.get("event") == "webhook_ingest_pending"
    ]
    assert ingest_pending_lines, "expected a webhook_ingest_pending log line"
    assert ingest_pending_lines[0]["correlation_id"] == "trace-webhook-1"


# ── watchdog re-binds the job's correlation ID (gap #1) ────────────────────────────


class _StubGenerationClient:
    def __init__(self, status: GenerationStatus) -> None:
        self._status = status

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        return self._status


async def test_watchdog_sweep_waiting_overdue_binds_job_correlation_id_into_log_lines() -> (
    None
):
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-watchdog-1",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        task_id="task-watchdog-1",
        conversion_id_1="conv-watchdog-1",
        eta=60,
        available_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=200),
        correlation_id="trace-watchdog-1",
    )  # type: ignore[arg-type]
    client = _StubGenerationClient(
        GenerationStatus(
            status=GENERATION_STATUS_COMPLETED,
            audio_url="http://musicgpt.test/audio/recovered",
            duration=181.0,
            title="Recovered",
        )
    )
    settings = Settings()

    await sweep_waiting_overdue(job, client=client, settings=settings)  # type: ignore[arg-type]

    recovered_lines = [
        line
        for line in _log_lines(stream)
        if line.get("event") == "watchdog_waiting_recovered"
    ]
    assert recovered_lines, "expected a watchdog_waiting_recovered log line"
    assert recovered_lines[0]["correlation_id"] == "trace-watchdog-1"


async def test_watchdog_sweep_waiting_overdue_resets_correlation_id_after_processing() -> (
    None
):
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-watchdog-2",
        seq=2,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        task_id="task-watchdog-2",
        conversion_id_1="conv-watchdog-2",
        eta=60,
        available_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=200),
        correlation_id="trace-watchdog-2",
    )  # type: ignore[arg-type]
    client = _StubGenerationClient(
        GenerationStatus(
            status=GENERATION_STATUS_COMPLETED,
            audio_url="http://musicgpt.test/audio/recovered-2",
            duration=181.0,
            title="Recovered 2",
        )
    )
    settings = Settings()

    await sweep_waiting_overdue(job, client=client, settings=settings)  # type: ignore[arg-type]

    assert correlation.get_correlation_id() is None
