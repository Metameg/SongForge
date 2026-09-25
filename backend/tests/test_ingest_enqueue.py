"""issue #14, criterion #1: a READY user song is enqueued onto `playback_queue`.

Mirrors `tests/test_ingest.py`'s London-school seam exactly (same fakes, same claimed-
job fixture) -- these tests drive the REAL, unmodified `ingest_claimed_job` through its
existing happy-path / self-heal / failure branches and assert on the `playback_queue`
table via the session, per the repo's "assert on externally observable state" testing
convention (no mock of `radio.queue` itself; the enqueue is a DB side effect).

RED phase (phase 1): `jobs.ingest._finalize_ready` does not yet call
`radio.queue.enqueue_song` -- every "enqueued" assertion below fails today because no
`playback_queue` row is ever created, not because anything fails to import/collect.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.ingest import AudioDownloadError, ingest_claimed_job
from songforge.models import (
    Base,
    Job,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    PlaybackQueue,
)

# See `tests/test_radio_queue.py`'s matching comment: `_finalize_ready`'s enqueue
# never sets `PlaybackQueue.id` itself (real Postgres generates it via `Identity()`),
# so the SQLite unit path needs the same `before_insert` shim as
# `tests/test_create_route.py`.
_ids = itertools.count(1)


def _assign_id(mapper: object, connection: object, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_ids)


@pytest.fixture(autouse=True)
def _sqlite_id_shim() -> Iterator[None]:
    event.listen(PlaybackQueue, "before_insert", _assign_id)
    yield
    event.remove(PlaybackQueue, "before_insert", _assign_id)


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


class _FakeStorage:
    def __init__(self, *, existing_keys: set[str] | None = None) -> None:
        self.existing = set(existing_keys or set())

    def exists(self, key: str) -> bool:
        return key in self.existing

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        self.existing.add(key)
        return f"https://cdn.test/{key}"


class _FakeDownloader:
    def __init__(self, url_to_result: dict[str, bytes | Exception]) -> None:
        self._url_to_result = url_to_result

    async def download(self, url: str) -> bytes:
        result = self._url_to_result.get(url)
        if result is None:
            raise AudioDownloadError(f"download failed for {url}")
        if isinstance(result, Exception):
            raise result
        return result


class _FakeGenerationClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call the generation client's create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("must not be called in these fixtures")


def _claimed_ingest_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="a song about testing",
        lyrics=None,
        state=JOB_STATE_INGEST_PENDING,
        attempts=0,
        available_at=datetime.now(timezone.utc),
        webhook_url="http://web:8000/api/generation/webhook",
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        eta=60,
        credit_estimate=1.0,
        audio_url="http://musicgpt.test/audio/hint-token",
        audio_duration=181.0,
        title="A Generated Song",
        ingest_attempts=0,
        song_id=None,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def test_happy_path_ready_enqueues_the_song_onto_playback_queue(
    session: AsyncSession,
) -> None:
    job = _claimed_ingest_job()
    storage = _FakeStorage()
    downloader = _FakeDownloader({job.audio_url: b"mp3-fake-bytes"})
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=_FakeGenerationClient(),  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )
    await session.commit()

    assert job.state == JOB_STATE_READY
    rows = (
        await session.scalars(
            select(PlaybackQueue).where(PlaybackQueue.song_id == job.conversion_id_1)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].job_id == job.job_id
    assert rows[0].played_at is None


async def test_idempotent_reingest_self_heal_also_enqueues_exactly_once(
    session: AsyncSession,
) -> None:
    """The self-heal path (object already in R2, job re-claimed still
    INGEST_PENDING) reaches READY for the first time too -- it must enqueue exactly
    like the fresh happy path, never zero times and never twice."""
    from songforge.storage import audio_key

    job = _claimed_ingest_job()
    key = audio_key(job.conversion_id_1)
    storage = _FakeStorage(existing_keys={key})
    downloader = _FakeDownloader({})
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=_FakeGenerationClient(),  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )
    await session.commit()

    assert job.state == JOB_STATE_READY
    rows = (
        await session.scalars(
            select(PlaybackQueue).where(PlaybackQueue.song_id == job.conversion_id_1)
        )
    ).all()
    assert len(rows) == 1


async def test_failed_download_does_not_enqueue_anything(
    session: AsyncSession,
) -> None:
    """A job that requeues/fails (never reaches READY) must never enqueue -- there is
    no playable song yet."""
    job = _claimed_ingest_job(ingest_attempts=0)
    storage = _FakeStorage()
    always_fails = _FakeDownloader({})
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=_FakeGenerationClient(),  # type: ignore[arg-type]
        downloader=always_fails,  # type: ignore[arg-type]
        settings=settings,
    )
    await session.commit()

    assert job.state == JOB_STATE_INGEST_PENDING
    rows = (await session.scalars(select(PlaybackQueue))).all()
    assert rows == []
