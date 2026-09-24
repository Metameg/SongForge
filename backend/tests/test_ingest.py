"""Unit tests for the ingest decision (issue #13, acceptance criteria #2-#4 + the
by-handle refresh + idempotent-re-ingest robustness criteria).

Mirrors `tests/test_dispatch.py`'s London-school seam: drives
`songforge.jobs.ingest.ingest_claimed_job` with a claimed (in-memory, no-session)
`Job` plus injected fakes for `storage`, `generation_client`, and `downloader` -- the
pure decision logic this module is built for (claiming via `FOR UPDATE SKIP LOCKED` is
`worker/ingest.py`'s DB concern, a later phase's I/O wiring, not tested here). A real
in-memory SQLite `session` IS supplied (unlike dispatch, which never touches a
session) because the ingest decision creates/queries `Song` rows -- the caller still
owns the final commit, per the repo's "pure decision, caller commits" convention.

Production change that turns these green: implementing `ingest_claimed_job` (and its
`AudioDownloadError`) in `songforge/jobs/ingest.py`, plus the new `Job` columns
(`audio_url`, `audio_duration`, `title`, `song_id`, `ingest_attempts`) in `models.py`
and migration `0004`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
)
from songforge.jobs.ingest import AudioDownloadError, ingest_claimed_job
from songforge.metrics import ingest_completed_total, ingest_failed_total, ingest_requeued_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    SOURCE_GENERATED,
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


class _FakeStorage:
    """File-local fake `ObjectStorage`: logs `exists`/`put` onto a shared `events`
    list so ordering against the downloader/generation-client fakes is observable."""

    def __init__(self, events: list[str], *, existing_keys: set[str] | None = None) -> None:
        self._events = events
        self.existing = set(existing_keys or set())
        self.put_calls: list[tuple[str, bytes]] = []

    def exists(self, key: str) -> bool:
        self._events.append(f"storage.exists:{key}")
        return key in self.existing

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        self._events.append(f"storage.put:{key}")
        self.put_calls.append((key, data))
        self.existing.add(key)
        return f"https://cdn.test/{key}"


class _FakeDownloader:
    """File-local fake downloader: `download(url)` returns the fixture bytes for
    `url`, or raises `AudioDownloadError` for any URL not in the fixture map
    (defaults every unfixtured URL to "fails" -- an empty map means "always fails")."""

    def __init__(
        self, events: list[str], url_to_result: dict[str, bytes | Exception]
    ) -> None:
        self._events = events
        self._url_to_result = url_to_result

    async def download(self, url: str) -> bytes:
        self._events.append(f"download:{url}")
        result = self._url_to_result.get(url)
        if result is None:
            raise AudioDownloadError(f"download failed for {url}")
        if isinstance(result, Exception):
            raise result
        return result


class _FakeGenerationClient:
    """File-local fake `GenerationClient`: `get_audio_url_by_id` returns/raises a
    fixed outcome; `create` explodes -- ingest must never submit a new generation."""

    def __init__(self, events: list[str], *, by_id_result: str | Exception) -> None:
        self._events = events
        self._by_id_result = by_id_result

    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call the generation client's create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        self._events.append(f"by_id:{task_id}")
        if isinstance(self._by_id_result, Exception):
            raise self._by_id_result
        return self._by_id_result


def _claimed_ingest_job(**overrides: object) -> Job:
    """A job already claimed + in `INGEST_PENDING` (this module's precondition)."""
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


# ── Happy path: download -> upload -> Song row -> READY (criteria #2, #3) ─────────


async def test_happy_path_downloads_uploads_and_marks_job_ready(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job()
    key = audio_key(job.conversion_id_1)
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {job.audio_url: b"mp3-fake-bytes"})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("by-id must not be called on the happy path")
    )
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    assert job.song_id == job.conversion_id_1
    assert not any(e.startswith("by_id:") for e in events)
    assert (key, b"mp3-fake-bytes") in [(k, d) for k, d in storage.put_calls]

    song = await session.get(Song, job.conversion_id_1)
    assert song is not None
    assert song.source == SOURCE_GENERATED
    assert song.object_key == key
    assert song.duration_seconds == 181
    assert song.title == "A Generated Song"


async def test_happy_path_checks_existence_before_downloading(
    session: AsyncSession,
) -> None:
    """Ordering IS part of the contract: a re-claim-safe ingest checks whether the
    canonical object already exists before ever attempting a download (criterion #7)."""
    events: list[str] = []
    job = _claimed_ingest_job()
    key = audio_key(job.conversion_id_1)
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {job.audio_url: b"bytes"})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert events[0] == f"storage.exists:{key}"
    assert events.index(f"storage.exists:{key}") < events.index(f"download:{job.audio_url}")


# ── By-handle refresh on an expired/failed hint URL (criterion #4) ────────────────


async def test_expired_hint_url_refreshes_via_by_id_and_then_succeeds(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job(audio_url="http://musicgpt.test/audio/expired-token")
    fresh_url = "http://musicgpt.test/audio/fresh-token"
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(
        events,
        {
            "http://musicgpt.test/audio/expired-token": AudioDownloadError("403 expired"),
            fresh_url: b"mp3-fresh-bytes",
        },
    )
    client = _FakeGenerationClient(events, by_id_result=fresh_url)
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    assert job.audio_url == fresh_url  # the hint was replaced by the refreshed URL
    assert events == [
        f"storage.exists:{audio_key(job.conversion_id_1)}",
        "download:http://musicgpt.test/audio/expired-token",
        "by_id:task-1",
        f"download:{fresh_url}",
        f"storage.put:{audio_key(job.conversion_id_1)}",
    ]
    song = await session.get(Song, job.conversion_id_1)
    assert song is not None


# ── Bounded retry / backoff on repeated failure (robustness criterion #5) ─────────


async def test_repeated_download_failure_requeues_with_backoff_below_the_cap(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=0)
    storage = _FakeStorage(events)
    always_fails = _FakeDownloader(events, {})  # every URL fails
    client = _FakeGenerationClient(
        events, by_id_result="http://musicgpt.test/audio/still-bad"
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)

    before = datetime.now(timezone.utc)
    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=always_fails,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING  # requeued, not FAILED yet
    assert job.ingest_attempts == 1
    assert job.available_at >= before + timedelta(seconds=20)
    assert await session.get(Song, job.conversion_id_1) is None  # nothing created


async def test_download_failure_exhausting_the_cap_marks_job_failed(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=2)  # one attempt away from the cap
    storage = _FakeStorage(events)
    always_fails = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result="http://musicgpt.test/audio/still-bad"
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=always_fails,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_FAILED
    assert job.ingest_attempts == 3
    assert await session.get(Song, job.conversion_id_1) is None


async def test_by_id_refresh_itself_failing_counts_as_a_failed_attempt(
    session: AsyncSession,
) -> None:
    """The by-id lookup is an external call too -- if IT fails (e.g. the generation
    API is briefly down), that must count as an exhausted attempt/requeue-with-
    backoff, not raise out of the ingest function."""
    events: list[str] = []
    job = _claimed_ingest_job()
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {})  # the hint URL fails
    client = _FakeGenerationClient(
        events, by_id_result=GenerationTransientError("byId is down")
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=15)

    before = datetime.now(timezone.utc)
    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.ingest_attempts == 1
    assert job.available_at >= before + timedelta(seconds=15)


# ── Idempotent / re-claim-safe ingest (criterion #7) ──────────────────────────────


async def test_idempotent_reingest_skips_download_when_object_already_uploaded(
    session: AsyncSession,
) -> None:
    """A re-claim after a crash between the R2 upload and the state-commit (the
    object is already there, but the job is still INGEST_PENDING) must not
    re-download/re-upload -- it self-heals straight to READY."""
    events: list[str] = []
    job = _claimed_ingest_job()
    key = audio_key(job.conversion_id_1)
    storage = _FakeStorage(events, existing_keys={key})
    downloader = _FakeDownloader(events, {})  # would raise if ever called
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    assert job.song_id == job.conversion_id_1
    assert not any(e.startswith("download:") for e in events)
    assert not any(e.startswith("storage.put:") for e in events)
    assert await session.get(Song, job.conversion_id_1) is not None  # self-healed


async def test_idempotent_reingest_does_not_duplicate_an_existing_song_row(
    session: AsyncSession,
) -> None:
    """A re-claim of a job whose Song row was already created (crash right before the
    job's own READY commit) must not attempt a second INSERT with the same id."""
    events: list[str] = []
    key = audio_key("conv-1")
    session.add(
        Song(
            id="conv-1",
            title="Existing",
            source=SOURCE_GENERATED,
            object_key=key,
            duration_seconds=181,
        )
    )
    await session.commit()

    job = _claimed_ingest_job(conversion_id_1="conv-1")
    storage = _FakeStorage(events, existing_keys={key})
    downloader = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    rows = (await session.scalars(select(Song).where(Song.id == "conv-1"))).all()
    assert len(rows) == 1


# ── Data-integrity guard: missing conversion_id_1 (phase-4 hardening) ─────────────


async def test_missing_conversion_id_1_marks_job_failed_without_touching_ports(
    session: AsyncSession,
) -> None:
    """`WAITING_FOR_WEBHOOK -> INGEST_PENDING` should never happen without
    `conversion_id_1` (dispatch sets it before that state), but a corrupted/legacy
    row must fail cleanly rather than crash the ingest loop or attempt any I/O."""
    events: list[str] = []
    job = _claimed_ingest_job(conversion_id_1=None)
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()
    before_failed = ingest_failed_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_FAILED
    assert job.song_id is None
    assert events == []  # never touched storage, downloader, or the generation client
    assert ingest_failed_total._value.get() == before_failed + 1


# ── By-id refresh failure: all three typed exceptions behave identically ──────────


@pytest.mark.parametrize(
    "by_id_exception",
    [
        GenerationRateLimited("byId is at capacity"),
        GenerationRejected(404, "unknown task_id"),
    ],
    ids=["rate_limited", "rejected"],
)
async def test_by_id_refresh_raising_rate_limited_or_rejected_counts_as_a_failed_attempt(
    session: AsyncSession, by_id_exception: Exception
) -> None:
    """Companion to `test_by_id_refresh_itself_failing_counts_as_a_failed_attempt`
    (which only covers `GenerationTransientError`): `ingest_claimed_job` catches
    `GenerationRateLimited`/`GenerationRejected`/`GenerationTransientError`
    identically in `_download_with_refresh` -- any of the three counts as a failed
    ingest attempt (requeued with backoff here, below the cap), never re-raised."""
    events: list[str] = []
    job = _claimed_ingest_job()
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {})  # the hint URL fails
    client = _FakeGenerationClient(events, by_id_result=by_id_exception)
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=15)

    before = datetime.now(timezone.utc)
    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.ingest_attempts == 1
    assert job.available_at >= before + timedelta(seconds=15)
    assert await session.get(Song, job.conversion_id_1) is None


# ── Counters: ingest_completed/failed/requeued increment on the right outcomes ────


async def test_ingest_completed_total_increments_on_happy_path(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job()
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {job.audio_url: b"bytes"})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()
    before = ingest_completed_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    assert ingest_completed_total._value.get() == before + 1


async def test_ingest_completed_total_increments_on_idempotent_reingest(
    session: AsyncSession,
) -> None:
    """The idempotent self-heal path (`storage.exists` true) is also a "completed"
    outcome -- it must count toward the same counter as a fresh download+upload."""
    events: list[str] = []
    job = _claimed_ingest_job()
    key = audio_key(job.conversion_id_1)
    storage = _FakeStorage(events, existing_keys={key})
    downloader = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()
    before = ingest_completed_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_READY
    assert ingest_completed_total._value.get() == before + 1


async def test_ingest_requeued_total_increments_below_the_cap(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=0)
    storage = _FakeStorage(events)
    always_fails = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result="http://musicgpt.test/audio/still-bad"
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=1)
    before = ingest_requeued_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=always_fails,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING
    assert ingest_requeued_total._value.get() == before + 1


async def test_duration_is_rounded_not_truncated(session: AsyncSession) -> None:
    """LOW finding: `_finalize_ready` used `int(...)` (truncation); `round(...)` is
    more correct for a duration column -- 181.6s should become 182, not 181."""
    events: list[str] = []
    job = _claimed_ingest_job(audio_duration=181.6)
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {job.audio_url: b"bytes"})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    song = await session.get(Song, job.conversion_id_1)
    assert song is not None
    assert song.duration_seconds == 182


# ── Catch-all: no unmapped exception may propagate and wedge the ingest loop ──────


class _ExplodingStorage:
    """A storage port whose `exists` raises an unexpected (unmapped) exception --
    simulating a boto3 `ClientError` or any other surprise. `put` must never be
    reached in these tests."""

    def exists(self, key: str) -> bool:
        raise RuntimeError("boom: unexpected storage error")

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        raise AssertionError("must not be called")


async def test_unmapped_exception_is_caught_and_requeues_with_backoff_below_the_cap(
    session: AsyncSession,
) -> None:
    """Critical quality-report finding: `ingest_claimed_job` had no catch-all
    (unlike `dispatch_claimed_job`), so any unmapped exception propagated out,
    crashed the drain loop, and -- because the same poisoned row (oldest `seq`) is
    always re-claimed first -- permanently wedged ingest for every worker instance.
    An unmapped exception must instead be bounded like any other failed attempt."""
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=0)
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    downloader = _FakeDownloader(events, {})
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)
    before = datetime.now(timezone.utc)

    await ingest_claimed_job(
        job,
        session=session,
        storage=_ExplodingStorage(),  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING  # requeued, not FAILED yet
    assert job.ingest_attempts == 1
    assert job.available_at >= before + timedelta(seconds=20)
    assert await session.get(Song, job.conversion_id_1) is None


async def test_unmapped_exception_exhausting_the_cap_marks_job_failed_not_propagated(
    session: AsyncSession,
) -> None:
    """Companion to the requeue case: repeated unmapped exceptions must eventually
    give up -> FAILED (never propagate), exactly like the download-failure path."""
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=2)  # one attempt away from the cap
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    downloader = _FakeDownloader(events, {})
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)

    await ingest_claimed_job(
        job,
        session=session,
        storage=_ExplodingStorage(),  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_FAILED
    assert job.ingest_attempts == 3
    assert await session.get(Song, job.conversion_id_1) is None


async def test_unmapped_exception_during_finalize_rolls_back_the_partial_flush(
    session: AsyncSession,
) -> None:
    """The security report's "poison-pill" scenario: an exception raised while
    staging/flushing the `Song` row (e.g. a real Postgres column-length violation on
    an oversized `title`) must roll back cleanly and still bound the retry -- not
    leave the session in a broken state that then fails the caller's own commit
    forever, and not leave the job stuck retrying without ever counting an attempt."""
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=0)
    storage = _FakeStorage(events)
    downloader = _FakeDownloader(events, {job.audio_url: b"bytes"})
    client = _FakeGenerationClient(
        events, by_id_result=AssertionError("must not be called")
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=20)

    async def _exploding_flush() -> None:
        raise RuntimeError("simulated flush-time integrity error")

    session.flush = _exploding_flush  # type: ignore[method-assign]

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.ingest_attempts == 1
    # The caller's own commit (mirroring `worker/ingest.py`'s drain loop) must still
    # succeed afterwards -- the rollback left the session usable, not broken.
    await session.commit()


async def test_ingest_failed_total_increments_on_attempt_exhaustion(
    session: AsyncSession,
) -> None:
    events: list[str] = []
    job = _claimed_ingest_job(ingest_attempts=2)  # one attempt away from the cap
    storage = _FakeStorage(events)
    always_fails = _FakeDownloader(events, {})
    client = _FakeGenerationClient(
        events, by_id_result="http://musicgpt.test/audio/still-bad"
    )
    settings = Settings(ingest_max_attempts=3, ingest_requeue_backoff_seconds=1)
    before = ingest_failed_total._value.get()

    await ingest_claimed_job(
        job,
        session=session,
        storage=storage,  # type: ignore[arg-type]
        generation_client=client,  # type: ignore[arg-type]
        downloader=always_fails,  # type: ignore[arg-type]
        settings=settings,
    )

    assert job.state == JOB_STATE_FAILED
    assert ingest_failed_total._value.get() == before + 1
