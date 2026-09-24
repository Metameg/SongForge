"""Edge-case coverage for `songforge.jobs.ingest.ingest_claimed_job` (issue #13,
phase-4 test-hardening) that documents -- rather than changes -- two boundary
inputs the main `test_ingest.py` suite doesn't exercise: a genuinely missing
(``None``) ``audio_url`` on an otherwise-valid ``INGEST_PENDING`` job, and a blank
(empty-string) ``conversion_id_1``.

Both are "should never happen" inputs under the normal webhook -> dispatch flow
(the webhook route never persists a ``None`` ``audio_url`` en route to
``INGEST_PENDING`` -- see `web/routes/webhook.py`'s
``body.conversion_path is None`` check; `jobs/dispatch.py` always sets a non-empty
``conversion_id_1`` before ``WAITING_FOR_WEBHOOK``). Per the phase-4 brief ("assert
it, don't change it"), these tests pin down exactly what the CURRENT implementation
does with a corrupted/legacy row, without modifying `jobs/ingest.py`. See each
test's docstring for a documented behavioral asymmetry worth a look in review.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.ingest import ingest_claimed_job
from songforge.models import (
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    Base,
    Job,
    Song,
)
from songforge.storage import audio_key


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


async def test_none_audio_url_on_an_ingest_pending_job_raises_assertion_error(
    session: AsyncSession,
) -> None:
    """CONCERN flagged for review (not fixed here): unlike the ``conversion_id_1 is
    None`` data-integrity guard in ``ingest_claimed_job`` (which gracefully marks
    the job FAILED -- see `test_ingest.py`'s
    `test_missing_conversion_id_1_marks_job_failed_without_touching_ports`), a
    ``None`` ``audio_url`` on an otherwise-valid ``INGEST_PENDING`` job is only
    caught by a bare ``assert`` in ``_download_with_refresh`` -- it raises
    ``AssertionError`` (uncaught by `ingest_claimed_job`) rather than failing the
    job gracefully, and ``assert`` statements are stripped entirely under
    ``python -O``. This can't happen through the normal webhook path today, but
    nothing in ``ingest_claimed_job`` enforces that invariant itself the way the
    parallel ``conversion_id_1`` guard does. This test pins down CURRENT behavior;
    it does not assert this is desired, and no production code is changed here."""
    job = _claimed_ingest_job(audio_url=None)

    with pytest.raises(AssertionError):
        await ingest_claimed_job(
            job,
            session=session,
            storage=_NeverCalledStorage(),  # type: ignore[arg-type]
            generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
            downloader=_NeverCalledDownloader(),  # type: ignore[arg-type]
            settings=Settings(),
        )


async def test_blank_conversion_id_1_is_treated_as_a_valid_song_id(
    session: AsyncSession,
) -> None:
    """``ingest_claimed_job``'s data-integrity guard checks ``is None``, not
    falsiness -- an empty-string ``conversion_id_1`` (never produced by
    `jobs/dispatch.py` today, but not type-guarded against here either) is NOT
    caught by that guard and proceeds as if it were a valid id: the object key
    becomes ``audio/.mp3`` and the ``Song`` row is created with ``id=""``.
    Documented here as current behavior; not a crash, but worth a look in review
    since it silently accepts a degenerate id rather than failing the job."""
    job = _claimed_ingest_job(conversion_id_1="")
    key = audio_key("")

    class _RecordingStorage:
        def __init__(self) -> None:
            self.put_calls: list[tuple[str, bytes]] = []

        def exists(self, k: str) -> bool:
            return False

        def put(self, k: str, data: bytes, content_type: str = "audio/mpeg") -> str:
            self.put_calls.append((k, data))
            return f"https://cdn.test/{k}"

    class _StubDownloader:
        async def download(self, url: str) -> bytes:
            return b"fake-bytes"

    storage = _RecordingStorage()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=_NeverCalledGenerationClient(),  # type: ignore[arg-type]
        downloader=_StubDownloader(),  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert job.state == JOB_STATE_READY
    assert job.song_id == ""
    assert storage.put_calls == [(key, b"fake-bytes")]
    song = await session.get(Song, "")
    assert song is not None
    assert song.object_key == key
