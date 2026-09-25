"""Unit tests for the watchdog decision units (issue #16, acceptance criteria A1-A5).

Mirrors `tests/test_dispatch.py`/`tests/test_ingest.py`'s London-school seam: the four
`claim_*` functions are driven against a real in-memory SQLite session (they are
genuine "SELECT ... FOR UPDATE SKIP LOCKED"-shaped queries, mirroring
`jobs.dispatch.claim_next_job`/`jobs.ingest.claim_next_ingest_job` -- fully
implemented and GREEN below); the four `sweep_*` decision functions are driven with a
claimed (in-memory) `Job` plus injected fakes for the generation client / rate limiter
/ notify hooks -- currently RED, because `jobs/watchdog.py`'s `sweep_*` bodies are
still no-ops (issue #16 phase 3 fills them in). See that module's docstring for the
full split rationale.

Production change that turns the `sweep_*` tests green: implementing
`sweep_waiting_overdue`/`sweep_submitting_stuck`/`sweep_ingest_overdue`/
`sweep_terminal_failures` in `songforge/jobs/watchdog.py`.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import songforge.jobs.watchdog as watchdog_module
from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_ERROR,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_IN_QUEUE,
    GenerationRateLimited,
    GenerationRejected,
    GenerationStatus,
    GenerationTransientError,
)
from songforge.jobs.watchdog import (
    claim_ingest_overdue_job,
    claim_submitting_stuck_job,
    claim_terminal_failure_job,
    claim_waiting_overdue_job,
    sweep_ingest_overdue,
    sweep_submitting_stuck,
    sweep_terminal_failures,
    sweep_waiting_overdue,
)
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_QUEUED,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
)

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: object, connection: object, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Iterator[None]:
    """See `tests/test_create_route.py`'s module docstring: SQLite can't server-
    generate `Job.seq` (a Postgres `Identity`), so this fills it in for this file's
    tests only."""
    event.listen(Job, "before_insert", _assign_test_seq)
    yield
    event.remove(Job, "before_insert", _assign_test_seq)


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _frozen_datetime(frozen: datetime) -> type[datetime]:
    """A `datetime` subclass whose `.now()` always returns `frozen` -- lets the
    boundary tests below assert the claim predicates' `<=` inclusivity EXACTLY
    (`overdue_at <= now`, `updated_at <= cutoff`) instead of relying on a margin wide
    enough to survive test-execution jitter, which can never prove which side of `<=`
    the comparison actually lands on."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:  # type: ignore[override]
            return frozen

    return _Frozen


def _job(**overrides: object) -> Job:
    """A generic job row; every field defaulted so each test only overrides what it
    cares about (mirrors `tests/test_ingest.py::_claimed_ingest_job`)."""
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-1",
        prompt="a song about testing",
        lyrics=None,
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        attempts=0,
        available_at=_now(),
        webhook_url="http://web:8000/api/generation/webhook",
        updated_at=_now(),
        client_ip="203.0.113.5",
        is_authenticated=False,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def _add(session: AsyncSession, job: Job) -> Job:
    """Add + commit `job` WITHOUT `session.refresh()`: SQLite (unlike Postgres) does
    not round-trip `tzinfo` through a `DateTime(timezone=True)` column, so a refresh
    would silently turn our tz-aware `updated_at`/`eta` fixtures naive and break the
    claim queries' aware-datetime arithmetic. `expire_on_commit=False` on the fixture
    session means the in-memory object (and its tz-aware attributes) stays exactly as
    constructed -- and a subsequent `select(Job)` in the SAME session returns this
    same identity-mapped instance rather than a freshly (naive-)deserialized one."""
    session.add(job)
    await session.commit()
    return job


# ═══════════════════════════════════════════════════════════════════════════════════
# Claims: FOR UPDATE SKIP LOCKED row-claim queries (A1) -- fully implemented, GREEN.
# ═══════════════════════════════════════════════════════════════════════════════════


class TestClaimWaitingOverdueJob:
    async def test_returns_a_row_overdue_past_eta_plus_buffer(
        self, session: AsyncSession
    ) -> None:
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        overdue = await _add(
            session,
            _job(
                job_id="overdue-1",
                eta=60,
                updated_at=_now() - timedelta(seconds=100),  # 100 > 60 + 30
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == overdue.job_id

    async def test_skips_a_row_still_within_its_eta_plus_buffer_window(
        self, session: AsyncSession
    ) -> None:
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="fresh-1",
                eta=120,
                updated_at=_now() - timedelta(seconds=10),  # nowhere near 120 + 30
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is None

    async def test_ignores_jobs_in_other_states(self, session: AsyncSession) -> None:
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="ready-1",
                state=JOB_STATE_INGEST_PENDING,
                eta=60,
                updated_at=_now() - timedelta(seconds=1000),
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is None

    async def test_returns_the_oldest_overdue_row_first(self, session: AsyncSession) -> None:
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=10)
        await _add(
            session,
            _job(job_id="newer", eta=10, updated_at=_now() - timedelta(seconds=50)),
        )
        await _add(
            session,
            _job(job_id="older", eta=10, updated_at=_now() - timedelta(seconds=500)),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "older"

    async def test_boundary_exactly_at_eta_plus_buffer_is_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D5's `overdue_at <= now` is inclusive: a row exactly AT the threshold, not
        just past it, must already be recoverable."""
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="exact-boundary",
                eta=60,
                updated_at=frozen - timedelta(seconds=90),  # 60 + 30, exactly
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "exact-boundary"

    async def test_boundary_one_second_before_eta_plus_buffer_is_not_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="one-second-early",
                eta=60,
                updated_at=frozen - timedelta(seconds=89),  # 1s short of 60 + 30
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is None

    async def test_eta_is_null_uses_the_buffer_alone_as_the_threshold(
        self, session: AsyncSession
    ) -> None:
        """A WAITING row with `eta IS NULL` shouldn't happen post-dispatch, but
        CONTEXT explicitly calls for a guard: `job.eta or 0` falls back to the buffer
        alone as the overdue threshold."""
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="no-eta-overdue",
                eta=None,
                updated_at=_now() - timedelta(seconds=40),  # > 0 + 30
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "no-eta-overdue"

    async def test_eta_is_null_still_respects_the_buffer_window(
        self, session: AsyncSession
    ) -> None:
        settings = Settings(watchdog_waiting_overdue_buffer_seconds=30)
        await _add(
            session,
            _job(
                job_id="no-eta-fresh",
                eta=None,
                updated_at=_now() - timedelta(seconds=5),  # well within the buffer
            ),
        )

        claimed = await claim_waiting_overdue_job(session, settings)

        assert claimed is None


class TestClaimSubmittingStuckJob:
    async def test_returns_a_task_id_less_row_past_the_lease(
        self, session: AsyncSession
    ) -> None:
        settings = Settings(watchdog_submitting_lease_seconds=60)
        await _add(
            session,
            _job(
                job_id="stuck-1",
                state=JOB_STATE_SUBMITTING,
                task_id=None,
                updated_at=_now() - timedelta(seconds=120),
            ),
        )

        claimed = await claim_submitting_stuck_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "stuck-1"

    async def test_skips_a_row_still_within_the_lease(self, session: AsyncSession) -> None:
        settings = Settings(watchdog_submitting_lease_seconds=60)
        await _add(
            session,
            _job(
                job_id="fresh-submit",
                state=JOB_STATE_SUBMITTING,
                task_id=None,
                updated_at=_now() - timedelta(seconds=5),
            ),
        )

        claimed = await claim_submitting_stuck_job(session, settings)

        assert claimed is None

    async def test_ignores_a_submitting_row_that_already_has_a_task_id(
        self, session: AsyncSession
    ) -> None:
        """A SUBMITTING row WITH a `task_id` isn't the crashed-mid-submit case at all
        -- it's just briefly between the API call returning and the WAITING_FOR_WEBHOOK
        commit; the watchdog must never touch it."""
        settings = Settings(watchdog_submitting_lease_seconds=60)
        await _add(
            session,
            _job(
                job_id="has-handle",
                state=JOB_STATE_SUBMITTING,
                task_id="task-1",
                updated_at=_now() - timedelta(seconds=120),
            ),
        )

        claimed = await claim_submitting_stuck_job(session, settings)

        assert claimed is None

    async def test_boundary_exactly_at_the_lease_is_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_submitting_lease_seconds=60)
        await _add(
            session,
            _job(
                job_id="lease-boundary",
                state=JOB_STATE_SUBMITTING,
                task_id=None,
                updated_at=frozen - timedelta(seconds=60),
            ),
        )

        claimed = await claim_submitting_stuck_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "lease-boundary"

    async def test_boundary_one_second_before_the_lease_is_not_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_submitting_lease_seconds=60)
        await _add(
            session,
            _job(
                job_id="lease-one-early",
                state=JOB_STATE_SUBMITTING,
                task_id=None,
                updated_at=frozen - timedelta(seconds=59),
            ),
        )

        claimed = await claim_submitting_stuck_job(session, settings)

        assert claimed is None


class TestClaimIngestOverdueJob:
    async def test_returns_a_row_past_the_generous_threshold(
        self, session: AsyncSession
    ) -> None:
        settings = Settings(watchdog_ingest_overdue_seconds=300)
        await _add(
            session,
            _job(
                job_id="stalled-ingest",
                state=JOB_STATE_INGEST_PENDING,
                updated_at=_now() - timedelta(seconds=600),
            ),
        )

        claimed = await claim_ingest_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "stalled-ingest"

    async def test_skips_a_row_within_the_threshold(self, session: AsyncSession) -> None:
        settings = Settings(watchdog_ingest_overdue_seconds=300)
        await _add(
            session,
            _job(
                job_id="normal-ingest",
                state=JOB_STATE_INGEST_PENDING,
                updated_at=_now() - timedelta(seconds=10),
            ),
        )

        claimed = await claim_ingest_overdue_job(session, settings)

        assert claimed is None

    async def test_boundary_exactly_at_the_threshold_is_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_ingest_overdue_seconds=300)
        await _add(
            session,
            _job(
                job_id="ingest-boundary",
                state=JOB_STATE_INGEST_PENDING,
                updated_at=frozen - timedelta(seconds=300),
            ),
        )

        claimed = await claim_ingest_overdue_job(session, settings)

        assert claimed is not None
        assert claimed.job_id == "ingest-boundary"

    async def test_boundary_one_second_before_the_threshold_is_not_claimed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frozen = _now()
        monkeypatch.setattr(watchdog_module, "datetime", _frozen_datetime(frozen))
        settings = Settings(watchdog_ingest_overdue_seconds=300)
        await _add(
            session,
            _job(
                job_id="ingest-one-early",
                state=JOB_STATE_INGEST_PENDING,
                updated_at=frozen - timedelta(seconds=299),
            ),
        )

        claimed = await claim_ingest_overdue_job(session, settings)

        assert claimed is None


class TestClaimTerminalFailureJob:
    async def test_returns_an_unhandled_failed_row(self, session: AsyncSession) -> None:
        await _add(
            session,
            _job(job_id="failed-1", state=JOB_STATE_FAILED, failure_handled_at=None),
        )

        claimed = await claim_terminal_failure_job(session)

        assert claimed is not None
        assert claimed.job_id == "failed-1"

    async def test_skips_an_already_handled_failed_row(self, session: AsyncSession) -> None:
        await _add(
            session,
            _job(job_id="failed-handled", state=JOB_STATE_FAILED, failure_handled_at=_now()),
        )

        claimed = await claim_terminal_failure_job(session)

        assert claimed is None

    async def test_ignores_jobs_in_non_failed_states(self, session: AsyncSession) -> None:
        await _add(session, _job(job_id="ready-2", state=JOB_STATE_INGEST_PENDING))

        claimed = await claim_terminal_failure_job(session)

        assert claimed is None


# ═══════════════════════════════════════════════════════════════════════════════════
# Decisions: sweep_* (A2/A3/A4) -- currently RED (no-op skeletons in jobs/watchdog.py).
# ═══════════════════════════════════════════════════════════════════════════════════


class _StubGenerationClient:
    """File-local fake `GenerationClient`: `get_status_by_id` returns/raises a fixed
    outcome; `create` explodes -- the watchdog must NEVER re-submit/re-charge a
    generation on recovery (A2's "poll ... without re-charging")."""

    def __init__(self, events: list[str], *, status: GenerationStatus | Exception) -> None:
        self._events = events
        self._status = status

    async def create(self, **kwargs: object) -> None:
        raise AssertionError("the watchdog must never call create() -- no re-charging")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("sweep_waiting_overdue must use get_status_by_id, not this")

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        self._events.append(f"by_id:{task_id}")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def _waiting_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        eta=60,
        credit_estimate=1.0,
        available_at=_now(),
        updated_at=_now() - timedelta(seconds=200),
        client_ip="203.0.113.5",
        is_authenticated=False,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class TestSweepWaitingOverdue:
    async def test_completed_status_recovers_the_job_without_recharging(self) -> None:
        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(
            events,
            status=GenerationStatus(
                status=GENERATION_STATUS_COMPLETED,
                audio_url="http://musicgpt.test/audio/recovered-token",
                duration=181.0,
                title="A Recovered Song",
            ),
        )
        settings = Settings()

        intent = await sweep_waiting_overdue(job, client=client, settings=settings)

        assert events == ["by_id:task-1"]  # the poll actually happened
        assert job.state == JOB_STATE_INGEST_PENDING
        assert job.audio_url == "http://musicgpt.test/audio/recovered-token"
        assert job.audio_duration == 181.0
        assert job.title == "A Recovered Song"
        assert intent == (settings.ingest_channel, job.job_id)

    async def test_error_status_marks_the_job_failed(self) -> None:
        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(
            events,
            status=GenerationStatus(
                status=GENERATION_STATUS_ERROR, audio_url=None, duration=None, title=None
            ),
        )
        settings = Settings()

        await sweep_waiting_overdue(job, client=client, settings=settings)

        assert job.state == JOB_STATE_FAILED

    async def test_failed_status_marks_the_job_failed(self) -> None:
        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(
            events,
            status=GenerationStatus(
                status=GENERATION_STATUS_FAILED, audio_url=None, duration=None, title=None
            ),
        )
        settings = Settings()

        await sweep_waiting_overdue(job, client=client, settings=settings)

        assert job.state == JOB_STATE_FAILED

    async def test_in_queue_status_leaves_the_job_waiting(self) -> None:
        """The job must be LEFT WAITING (not moved into a bad state) -- but the poll
        itself must still have happened (proving this isn't just "never touched it")."""
        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(
            events,
            status=GenerationStatus(
                status=GENERATION_STATUS_IN_QUEUE, audio_url=None, duration=None, title=None
            ),
        )
        settings = Settings()

        await sweep_waiting_overdue(job, client=client, settings=settings)

        assert events == ["by_id:task-1"]
        assert job.state == JOB_STATE_WAITING_FOR_WEBHOOK

    @pytest.mark.parametrize(
        "exc",
        [
            GenerationRateLimited("byId is at capacity"),
            GenerationTransientError("byId returned 503"),
            GenerationRejected(404, "unknown task_id"),
        ],
        ids=["rate_limited", "transient", "rejected"],
    )
    async def test_a_byid_poll_failure_is_never_raised_and_leaves_the_job_waiting(
        self, exc: Exception
    ) -> None:
        """F1: an outage of the generation API's `/byId` endpoint (429/5xx/timeout/
        terminal 4xx, the same typed exceptions `dispatch_claimed_job` reacts to) must
        NEVER propagate out of `sweep_waiting_overdue` -- an uncaught raise here would
        unwind through `_run_sweeps`' drain loop and abort the whole tick, starving the
        other three sweeps (including the terminal-failure refund) for a full poll
        interval, precisely during the outage the watchdog exists to survive. The job
        is left WAITING untouched: no state change, no re-charge, no notify intent."""
        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(events, status=exc)
        settings = Settings()

        intent = await sweep_waiting_overdue(job, client=client, settings=settings)

        assert events == ["by_id:task-1"]  # the poll was actually attempted
        assert job.state == JOB_STATE_WAITING_FOR_WEBHOOK  # left untouched
        assert intent is None  # no notify fires for an inconclusive poll

    async def test_completed_with_missing_audio_url_still_transitions_but_ingest_catches_it(
        self, session: AsyncSession
    ) -> None:
        """A malformed COMPLETED `/byId` response (no `audio_url`) has NO dedicated
        guard in `sweep_waiting_overdue` -- unlike `get_audio_url_by_id`, which raises
        on exactly this shape. It transitions to INGEST_PENDING with `audio_url=None`
        exactly like a well-formed COMPLETED. Proven SAFE, not asserted as a defect:
        `jobs.ingest.ingest_claimed_job`'s own data-integrity guard
        (`job.audio_url is None`) marks the very next claim of this row FAILED rather
        than attempting a download with no URL -- this test drives that interaction
        end to end so the two modules' assumptions are checked together, not just each
        one's own docstring claim."""
        from songforge.jobs.ingest import ingest_claimed_job

        events: list[str] = []
        job = _waiting_job()
        client = _StubGenerationClient(
            events,
            status=GenerationStatus(
                status=GENERATION_STATUS_COMPLETED,
                audio_url=None,
                duration=None,
                title=None,
            ),
        )
        settings = Settings()

        await sweep_waiting_overdue(job, client=client, settings=settings)

        assert job.state == JOB_STATE_INGEST_PENDING
        assert job.audio_url is None

        class _NullDownloader:
            async def download(self, url: str) -> bytes:
                raise AssertionError("must never attempt a download with no audio_url")

        class _NullStorage:
            def exists(self, key: str) -> bool:
                return False

            def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
                raise AssertionError("must never upload with no audio_url")

        await ingest_claimed_job(
            job,
            session=session,
            storage=_NullStorage(),  # type: ignore[arg-type]
            generation_client=client,  # type: ignore[arg-type]
            downloader=_NullDownloader(),  # type: ignore[arg-type]
            settings=settings,
        )

        assert job.state == JOB_STATE_FAILED


def _submitting_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_SUBMITTING,
        task_id=None,
        attempts=0,
        available_at=_now(),
        updated_at=_now() - timedelta(seconds=300),
        client_ip="203.0.113.5",
        is_authenticated=False,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class TestSweepSubmittingStuck:
    async def test_requeues_to_queued_with_bumped_attempts_and_returns_a_notify_intent(
        self,
    ) -> None:
        job = _submitting_job(attempts=1)
        settings = Settings()

        intent = await sweep_submitting_stuck(job, settings=settings)

        assert job.state == JOB_STATE_QUEUED
        assert job.attempts == 2
        assert intent == (settings.jobs_new_channel, job.job_id)

    async def test_requeue_resets_available_at_to_now_for_immediate_reclaim(self) -> None:
        """This is a crash-recovery re-queue, not a backoff (D6: the lease age-gate
        already bounded the delay) -- `available_at` must move to NOW, not stay at its
        stale pre-crash value or get pushed further out."""
        job = _submitting_job(available_at=_now() - timedelta(seconds=999))
        settings = Settings()
        before = _now()

        await sweep_submitting_stuck(job, settings=settings)

        assert job.available_at is not None
        assert job.available_at >= before


def _ingest_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_INGEST_PENDING,
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        audio_url="http://musicgpt.test/audio/hint-token",
        ingest_attempts=0,
        available_at=_now() - timedelta(seconds=600),
        updated_at=_now() - timedelta(seconds=600),
        client_ip="203.0.113.5",
        is_authenticated=False,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class TestSweepIngestOverdue:
    async def test_nudges_available_at_to_now_for_re_claim(self) -> None:
        job = _ingest_job(ingest_attempts=1)
        settings = Settings(ingest_max_attempts=5)
        before = _now()

        await sweep_ingest_overdue(job, settings=settings)

        assert job.state == JOB_STATE_INGEST_PENDING
        assert job.available_at <= before + timedelta(seconds=1)

    async def test_escalates_to_failed_once_ingest_attempts_is_at_the_ceiling(self) -> None:
        job = _ingest_job(ingest_attempts=5)
        settings = Settings(ingest_max_attempts=5)

        await sweep_ingest_overdue(job, settings=settings)

        assert job.state == JOB_STATE_FAILED

    async def test_boundary_one_below_the_ceiling_still_nudges_not_escalates(self) -> None:
        """`>=` is the escalation predicate -- one below the ceiling must still take
        the nudge branch, not the give-up branch."""
        job = _ingest_job(ingest_attempts=4)
        settings = Settings(ingest_max_attempts=5)
        before = _now()

        await sweep_ingest_overdue(job, settings=settings)

        assert job.state == JOB_STATE_INGEST_PENDING
        assert job.available_at >= before
        assert job.ingest_attempts == 4  # the nudge never touches this counter


def _failed_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-abc",
        prompt="p",
        state=JOB_STATE_FAILED,
        client_ip="203.0.113.9",
        is_authenticated=False,
        failure_handled_at=None,
        created_at=_now(),
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class TestSweepTerminalFailures:
    """`sweep_terminal_failures` is now a PURE decision step (F4): it stamps
    `failure_handled_at` and returns a `TerminalFailureIntent` for the caller
    (`worker/watchdog.py::_drain_terminal_failures`) to refund + notify with AFTER
    that stamp commits -- it no longer calls a `RateLimiter` or a publish hook itself.
    The post-commit refund/notify firing (and its best-effort Redis-blip handling) is
    covered in `tests/test_worker_watchdog.py`."""

    async def test_returns_the_reconstructed_identity_and_ip(self) -> None:
        job = _failed_job()

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.identity.user_id == "user-abc"
        assert intent.identity.is_authenticated is False
        assert intent.ip == "203.0.113.9"

    async def test_intent_carries_the_user_id_and_job_id_for_the_notification(self) -> None:
        job = _failed_job()

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.user_id == "user-abc"
        assert intent.job_id == "job-1"

    async def test_stamps_failure_handled_at(self) -> None:
        job = _failed_job()
        before = _now()

        await sweep_terminal_failures(job)

        assert job.failure_handled_at is not None
        assert job.failure_handled_at >= before

    async def test_never_calls_the_generation_client(self) -> None:
        """A4 (never auto-regenerates): `sweep_terminal_failures` isn't even handed a
        generation client -- there is no port to call. This test documents that
        contract by never transitioning the job to QUEUED/SUBMITTING."""
        job = _failed_job()

        await sweep_terminal_failures(job)

        assert job.state == JOB_STATE_FAILED  # never transitioned to QUEUED/SUBMITTING

    async def test_returns_none_and_does_not_restamp_an_already_handled_row(self) -> None:
        """Defensive (F4): the claim guard (`failure_handled_at IS NULL`) should make
        this unreachable in production, but a row handed in already stamped must not
        be re-stamped or hand back a stale intent."""
        already_handled = _now() - timedelta(seconds=60)
        job = _failed_job(failure_handled_at=already_handled)

        intent = await sweep_terminal_failures(job)

        assert intent is None
        assert job.failure_handled_at == already_handled

    async def test_ip_is_the_empty_string_when_client_ip_is_none(self) -> None:
        job = _failed_job(client_ip=None)

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.ip == ""

    async def test_ip_is_the_empty_string_when_client_ip_is_the_empty_string(self) -> None:
        job = _failed_job(client_ip="")

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.ip == ""

    async def test_reconstructs_an_authenticated_identity(self) -> None:
        """D3: `is_authenticated` is always False today (no accounts system yet), but
        the refund shape must already be correct once accounts land -- driven here with
        a forward-looking `is_authenticated=True` job."""
        job = _failed_job(is_authenticated=True)

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.identity.is_authenticated is True


class TestSweepTerminalFailuresChargeDay:
    """F3: the refund's day bucket is the job's CREATE day (`consume`'s charged
    bucket), never "today" -- a job created before a UTC-midnight rollover and swept
    after it must refund the day it actually charged, closing a cross-midnight
    farmable-gap (see `RateLimiter.refund`'s `day` docstring)."""

    async def test_day_is_the_jobs_created_at_utc_date(self) -> None:
        created = datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)
        job = _failed_job(created_at=created)

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.day == "2026-01-01"

    async def test_day_survives_a_cross_midnight_sweep_not_todays_date(self) -> None:
        """The row is swept well after its create day has rolled over -- `intent.day`
        must still be the CREATE day, not whatever "today" is at sweep time."""
        created = datetime.now(timezone.utc) - timedelta(days=2)
        job = _failed_job(created_at=created)

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.day == created.date().isoformat()
        assert intent.day != _now().date().isoformat()

    async def test_day_normalizes_a_naive_created_at(self) -> None:
        """SQLite doesn't round-trip `tzinfo` (see `_as_aware_utc`'s docstring in
        `jobs/watchdog.py`) -- a naive `created_at` must still normalize to the
        correct UTC date rather than raising."""
        job = _failed_job(created_at=datetime(2026, 3, 15, 12, 0))  # naive, no tzinfo

        intent = await sweep_terminal_failures(job)

        assert intent is not None
        assert intent.day == "2026-03-15"


# ═══════════════════════════════════════════════════════════════════════════════════
# A1 idempotency: a re-claim of an already-handled FAILED row never double-acts.
# ═══════════════════════════════════════════════════════════════════════════════════


class TestSweepTerminalFailuresIdempotency:
    async def test_a_second_claim_after_the_stamp_finds_nothing_to_hand_back(
        self, session: AsyncSession
    ) -> None:
        """The idempotency guarantee (D2) is row-claim-based, not a check inside
        `sweep_terminal_failures` itself: `claim_terminal_failure_job`'s `WHERE
        failure_handled_at IS NULL` is what makes a second sweep pass over the SAME
        row a no-op. Drives the real claim + sweep + commit twice over one job to
        prove that end to end -- exactly one intent handed back, ever, no matter how
        many watchdog ticks later a second sweep pass runs."""
        await _add(session, _job(job_id="failed-1", state=JOB_STATE_FAILED))

        # First sweep: claims the row, stamps `failure_handled_at`, hands back an
        # intent for the caller to refund+notify with.
        job = await claim_terminal_failure_job(session)
        assert job is not None
        intent = await sweep_terminal_failures(job)
        await session.commit()

        assert intent is not None

        # Second sweep (a later watchdog tick, or a concurrent worker instance): the
        # claim query itself finds nothing left to hand back.
        second_claim = await claim_terminal_failure_job(session)

        assert second_claim is None


# ═══════════════════════════════════════════════════════════════════════════════════
# A3 interaction regression (issue #16 step 7): a webhook that arrives AFTER the
# watchdog (not ingest) has already moved a job out of WAITING_FOR_WEBHOOK is still a
# no-op -- `receive_webhook`'s existing state-guard doesn't care which recovery path
# made the transition.
# ═══════════════════════════════════════════════════════════════════════════════════


def _webhook_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = dict(
        subtype="music_ai",
        task_id="task-1",
        conversion_id="conv-1",
        conversion_path="http://musicgpt.test/audio/stale-hint-token",
        conversion_duration=999.0,
        title="A Stale Late Webhook",
        status=None,
    )
    body.update(overrides)
    return body


async def test_late_webhook_after_watchdog_recovery_is_a_noop() -> None:
    """`sweep_waiting_overdue` recovers an overdue job via the `/byId` poll (state ->
    INGEST_PENDING, no re-charge). The job's ORIGINAL webhook then arrives late.
    `receive_webhook`'s existing `job.state != WAITING_FOR_WEBHOOK` no-op guard
    (`web/routes/webhook.py`) must still hold -- proving it doesn't matter whether the
    state change that moved the job out of WAITING_FOR_WEBHOOK came from `jobs.ingest`
    (the case the existing coverage in `tests/test_idempotency_guards_a3.py`/
    `tests/test_webhook_route.py` drives) or from the watchdog.

    Builds its own engine/sessionmaker (rather than this file's shared `session`
    fixture) so the webhook route's own DB-session dependency -- driven through a real
    `TestClient` request, on its own portal -- opens fresh sessions off the same
    in-memory database instead of reusing a session object live across two different
    execution contexts.
    """
    from songforge.web.app import create_app
    from songforge.web.routes.webhook import get_notify_dependency as get_webhook_notify
    from songforge.web.routes.webhook import get_session as get_webhook_session

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with engine_sessionmaker() as session:
        session.add(_waiting_job())
        await session.commit()

    settings = Settings()
    client = _StubGenerationClient(
        [],
        status=GenerationStatus(
            status=GENERATION_STATUS_COMPLETED,
            audio_url="http://musicgpt.test/audio/recovered-token",
            duration=181.0,
            title="A Recovered Song",
        ),
    )
    async with engine_sessionmaker() as session:
        claimed = await claim_waiting_overdue_job(session, settings)
        assert claimed is not None
        await sweep_waiting_overdue(claimed, client=client, settings=settings)
        await session.commit()

    class _NotifySpy:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def __call__(self, channel: str, payload: str) -> None:
            self.calls.append((channel, payload))

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with engine_sessionmaker() as session:
            yield session

    notify_spy = _NotifySpy()
    app = create_app()
    app.dependency_overrides[get_webhook_session] = _override_session
    app.dependency_overrides[get_webhook_notify] = lambda: notify_spy
    http_client = TestClient(app)

    resp = http_client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert notify_spy.calls == []  # no-op: no re-transition, no double-NOTIFY

    async with engine_sessionmaker() as session:
        row = (await session.scalars(select(Job).where(Job.job_id == "job-1"))).one()
        assert row.state == JOB_STATE_INGEST_PENDING  # unchanged by the late webhook
        # The watchdog's recovered value survives -- the stale late webhook never
        # overwrote it.
        assert row.audio_url == "http://musicgpt.test/audio/recovered-token"
