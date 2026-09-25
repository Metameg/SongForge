"""Issue #16 acceptance A3 regression guards: "duplicate/late webhooks are idempotent
... a song is never enqueued twice."

These guarantees were already BUILT for issues #13/#14 -- see
`web/routes/webhook.py::receive_webhook`'s idempotency no-op branch (a webhook for a
job no longer `WAITING_FOR_WEBHOOK` is a 200 no-op) and
`jobs/ingest.py::_finalize_ready`'s pre-enqueue/pre-insert checks -- and are already
exercised by:

  - `tests/test_webhook_route.py::test_duplicate_webhook_after_a_successful_one_does_not_re_notify`
  - `tests/test_webhook_route.py::test_webhook_is_a_noop_for_a_job_already_past_waiting_for_webhook`
  - `tests/test_ingest.py::test_idempotent_reingest_skips_download_when_object_already_uploaded`
  - `tests/test_ingest.py::test_idempotent_reingest_does_not_duplicate_an_existing_song_row`

This file pins them again as EXPLICIT issue-#16 acceptance guards (A3: "crashed-worker
rows are re-claimed and duplicate/late webhooks are idempotent -- a song is never
enqueued twice") -- a regression here is a #16 regression, not just a #13/#14 one
from a slightly different angle than the existing coverage: driving
`ingest_claimed_job` twice in a row over the SAME session/job object (simulating a
watchdog re-claim of a job that already reached READY, rather than a fresh `Job`
built with `storage.exists` pre-seeded), and driving the webhook route three times in
a row (delayed + duplicate + a second duplicate) rather than twice.

STATUS: both tests below are GREEN already -- issue #16 doesn't need to BUILD
anything new for A3, only verify it holds. Kept as their own file (rather than folded
into `test_webhook_route.py`/`test_ingest.py`) so a `git blame`/CI-failure on this
file unambiguously points at a #16 acceptance regression.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.ingest import ingest_claimed_job
from songforge.models import (
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
    PlaybackQueue,
    Song,
)
from songforge.web.app import create_app
from songforge.web.routes.webhook import get_notify_dependency, get_session

_ids = itertools.count(1)


def _assign_id(mapper: object, connection: object, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_ids)


@pytest.fixture(autouse=True)
def _sqlite_id_shim() -> Iterator[None]:
    """See `tests/test_ingest.py`'s matching fixture: SQLite doesn't auto-populate a
    `BigInteger` `Identity()` PK (`PlaybackQueue.id`)."""
    event.listen(PlaybackQueue, "before_insert", _assign_id)
    yield
    event.remove(PlaybackQueue, "before_insert", _assign_id)


class _FakeStorage:
    def __init__(self, *, existing_keys: set[str] | None = None) -> None:
        self.existing = set(existing_keys or set())
        self.put_calls: list[tuple[str, bytes]] = []

    def exists(self, key: str) -> bool:
        return key in self.existing

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        self.put_calls.append((key, data))
        self.existing.add(key)
        return f"https://cdn.test/{key}"


class _NeverCalledDownloader:
    async def download(self, url: str) -> bytes:
        raise AssertionError("a re-claim that already has the object must not download")


class _NeverCalledGenerationClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("a re-claim that already has the object must not refresh")


async def test_reclaiming_an_already_ready_job_never_double_enqueues_the_playback_queue() -> (
    None
):
    """A3: simulates the watchdog re-claiming a job that a prior worker already
    finished (crash-after-commit, or simply two overlapping sweeps) by calling
    `ingest_claimed_job` twice over the SAME `session`/`Job` object. The second call
    must self-heal (object already in storage, `Song` row already present) without
    inserting a second `playback_queue` row for the same `job_id`."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    job = Job(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="a song about testing",
        state=JOB_STATE_INGEST_PENDING,
        available_at=datetime.now(timezone.utc),
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        audio_url="http://musicgpt.test/audio/hint-token",
        ingest_attempts=0,
    )
    storage = _FakeStorage()
    downloader_first = _FakeStorageDownloader(b"mp3-bytes")
    settings = Settings()

    async with sessionmaker() as session:
        await ingest_claimed_job(
            job,
            session=session,
            storage=storage,  # type: ignore[arg-type]
            generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
            downloader=downloader_first,  # type: ignore[arg-type]
            settings=settings,
        )
        await session.commit()
        assert job.state == JOB_STATE_READY

        # Re-claim: same job, same session, storage already has the object -- the
        # watchdog's "crashed-worker row re-claimed" case (A3).
        await ingest_claimed_job(
            job,
            session=session,
            storage=storage,  # type: ignore[arg-type]
            generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
            downloader=_NeverCalledDownloader(),  # type: ignore[arg-type]
            settings=settings,
        )
        await session.commit()

        assert job.state == JOB_STATE_READY
        rows = (
            await session.scalars(
                select(PlaybackQueue).where(PlaybackQueue.job_id == "job-1")
            )
        ).all()
        assert len(rows) == 1  # never enqueued twice

        songs = (await session.scalars(select(Song).where(Song.id == "conv-1"))).all()
        assert len(songs) == 1  # never double-inserted


class _FakeStorageDownloader:
    """Returns fixed bytes for any URL -- used only for the FIRST (real) download in
    the re-claim test above; the second call must never reach a downloader at all."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def download(self, url: str) -> bytes:
        return self._body


async def test_three_webhook_deliveries_for_the_same_task_notify_and_transition_exactly_once() -> (
    None
):
    """A3: a delayed original + two duplicate/late redeliveries (the simulator's
    DELAYED_WEBHOOK/DUPLICATE_WEBHOOK faults, `.orchestrator/CONTEXT.md`) for the SAME
    `task_id` must transition the job and NOTIFY exactly once -- never re-enter
    INGEST_PENDING, never double-NOTIFY, regardless of how many times it's redelivered."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    _seq = itertools.count(1)

    def _assign_seq(mapper: Any, connection: Any, target: Job) -> None:
        if target.seq is None:
            target.seq = next(_seq)

    event.listen(Job, "before_insert", _assign_seq)
    try:
        async with sessionmaker() as session:
            session.add(
                Job(
                    job_id="job-1",
                    user_id="user-1",
                    prompt="p",
                    state=JOB_STATE_WAITING_FOR_WEBHOOK,
                    task_id="task-1",
                    conversion_id_1="conv-1",
                    conversion_id_2="conv-2",
                    webhook_url="http://web:8000/api/generation/webhook",
                )
            )
            await session.commit()

        class _NotifySpy:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def __call__(self, channel: str, payload: str) -> None:
                self.calls.append((channel, payload))

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                yield session

        notify_spy = _NotifySpy()
        app = create_app()
        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
        client = TestClient(app)

        body = {
            "subtype": "music_ai",
            "task_id": "task-1",
            "conversion_id": "conv-1",
            "conversion_path": "http://musicgpt.test/audio/hint-token",
            "conversion_duration": 181.0,
            "title": "A Generated Song",
            "status": None,
        }

        responses = [client.post("/api/generation/webhook", json=body) for _ in range(3)]

        assert all(resp.status_code == 200 for resp in responses)
        assert len(notify_spy.calls) == 1  # exactly one NOTIFY across 3 deliveries

        async with sessionmaker() as session:
            job = (
                await session.scalars(select(Job).where(Job.job_id == "job-1"))
            ).one()
            assert job.state == JOB_STATE_INGEST_PENDING  # never re-transitioned
    finally:
        event.remove(Job, "before_insert", _assign_seq)
