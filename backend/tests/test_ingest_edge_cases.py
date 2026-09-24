"""Edge-case coverage for `songforge.jobs.ingest.ingest_claimed_job` (issue #13) for
two boundary inputs the main `test_ingest.py` suite doesn't exercise: a genuinely
missing (``None``) ``audio_url`` on an otherwise-valid ``INGEST_PENDING`` job, and a
blank (empty-string) ``conversion_id_1``.

Both are "should never happen" inputs under the normal webhook -> dispatch flow
(the webhook route never persists a ``None`` ``audio_url`` en route to
``INGEST_PENDING`` -- see `web/routes/webhook.py`'s
``body.conversion_path is None`` check; `jobs/dispatch.py` always sets a non-empty
``conversion_id_1`` before ``WAITING_FOR_WEBHOOK``). Phase-4 pinned these down as
documented CONCERNS (a bare ``assert`` for the first, an ``is None`` guard that
missed empty-string for the second); phase-5 review fixed both (quality report HIGH
and MED findings) -- this file now asserts the FIXED graceful-FAILED behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.ingest import ingest_claimed_job
from songforge.metrics import ingest_failed_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    Base,
    Job,
    Song,
)


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


class _NeverCalledStorage:
    def exists(self, key: str) -> bool:
        return False

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        raise AssertionError(f"must not upload {key!r}")


class _NeverCalledDownloader:
    async def download(self, url: str) -> bytes:
        raise AssertionError(f"must not download {url!r}")


class _NeverCalledGenerationClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("must not be called")


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


async def test_none_audio_url_on_an_ingest_pending_job_marks_job_failed_gracefully(
    session: AsyncSession,
) -> None:
    """FIXED (quality report HIGH finding): a ``None`` ``audio_url`` on an otherwise-
    valid ``INGEST_PENDING`` job used to be caught only by a bare ``assert`` in
    ``_download_with_refresh`` -- an uncaught ``AssertionError`` (stripped entirely
    under ``python -O``) that would have wedged the whole ingest loop (no catch-all
    existed either). ``ingest_claimed_job`` now guards this explicitly, mirroring the
    ``conversion_id_1`` guard: mark the job FAILED, touch no I/O port, and return
    normally -- this can't happen through the normal webhook path today, but a
    corrupted/legacy row must fail cleanly rather than crash the ingest loop."""
    job = _claimed_ingest_job(audio_url=None)
    before_failed = ingest_failed_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=_NeverCalledStorage(),  # type: ignore[arg-type]
        generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
        downloader=_NeverCalledDownloader(),  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert job.state == JOB_STATE_FAILED
    assert job.song_id is None
    assert ingest_failed_total._value.get() == before_failed + 1


async def test_blank_conversion_id_1_marks_job_failed_instead_of_a_degenerate_id(
    session: AsyncSession,
) -> None:
    """FIXED (quality report MED finding): the ``conversion_id_1`` data-integrity
    guard used to check ``is None``, not falsiness, so an empty-string
    ``conversion_id_1`` (never produced by `jobs/dispatch.py` today, but not type-
    guarded against here either) sailed past it and produced a degenerate
    ``Song(id="")`` at key ``audio/.mp3``. The guard now uses falsiness
    (``not job.conversion_id_1``), so a blank id is rejected exactly like a missing
    one -- no I/O port is touched and no ``Song`` row is created."""
    job = _claimed_ingest_job(conversion_id_1="")

    await ingest_claimed_job(
        job,
        session=session,
        storage=_NeverCalledStorage(),  # type: ignore[arg-type]
        generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
        downloader=_NeverCalledDownloader(),  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert job.state == JOB_STATE_FAILED
    assert job.song_id is None
    assert await session.get(Song, "") is None
