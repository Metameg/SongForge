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

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_ERROR,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_IN_QUEUE,
    GenerationStatus,
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
from songforge.web.rate_limit import Identity

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
        notified: list[str] = []

        async def _notify(channel: str, payload: str) -> None:
            notified.append(payload)

        await sweep_waiting_overdue(
            job, client=client, settings=settings, notify_ingest=_notify
        )

        assert events == ["by_id:task-1"]  # the poll actually happened
        assert job.state == JOB_STATE_INGEST_PENDING
        assert job.audio_url == "http://musicgpt.test/audio/recovered-token"
        assert job.audio_duration == 181.0
        assert job.title == "A Recovered Song"
        assert notified == [job.job_id]

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
    async def test_requeues_to_queued_with_bumped_attempts_and_notifies_new_job(
        self,
    ) -> None:
        job = _submitting_job(attempts=1)
        settings = Settings()
        notified: list[str] = []

        async def _notify(channel: str, payload: str) -> None:
            notified.append(payload)

        await sweep_submitting_stuck(job, settings=settings, notify_new_job=_notify)

        assert job.state == JOB_STATE_QUEUED
        assert job.attempts == 2
        assert notified == [job.job_id]


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


class _RecordingRateLimiter:
    """File-local fake `RateLimiter`: only `refund` is exercised by the terminal-
    failure sweep -- records `(identity, ip)` so the test can assert the EXACT
    reconstructed identity/ip (D3: both the cookie AND ip legs must be refunded for an
    anon job)."""

    def __init__(self) -> None:
        self.refund_calls: list[tuple[Identity, str]] = []

    async def consume(self, identity: Identity, ip: str) -> None:
        raise AssertionError("sweep_terminal_failures must never consume a new slot")

    async def remaining(self, identity: Identity, ip: str) -> int:
        raise AssertionError("sweep_terminal_failures must never read `remaining`")

    async def refund(self, identity: Identity, ip: str) -> None:
        self.refund_calls.append((identity, ip))


def _failed_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-abc",
        prompt="p",
        state=JOB_STATE_FAILED,
        client_ip="203.0.113.9",
        is_authenticated=False,
        failure_handled_at=None,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class TestSweepTerminalFailures:
    async def test_refunds_the_reconstructed_identity_and_ip(self) -> None:
        job = _failed_job()
        rate_limiter = _RecordingRateLimiter()

        await sweep_terminal_failures(job, rate_limiter=rate_limiter)  # type: ignore[arg-type]

        assert len(rate_limiter.refund_calls) == 1
        identity, ip = rate_limiter.refund_calls[0]
        assert identity.user_id == "user-abc"
        assert identity.is_authenticated is False
        assert ip == "203.0.113.9"

    async def test_publishes_a_job_failed_notification_on_the_users_channel(self) -> None:
        job = _failed_job()
        rate_limiter = _RecordingRateLimiter()
        published: list[tuple[str, str]] = []

        async def _publish(user_id: str, message: str) -> None:
            published.append((user_id, message))

        await sweep_terminal_failures(
            job, rate_limiter=rate_limiter, publish_user_event=_publish  # type: ignore[arg-type]
        )

        assert len(published) == 1
        user_id, message = published[0]
        assert user_id == "user-abc"
        assert "job-1" in message  # the job id is identifiable in the notification

    async def test_stamps_failure_handled_at(self) -> None:
        job = _failed_job()
        rate_limiter = _RecordingRateLimiter()
        before = _now()

        await sweep_terminal_failures(job, rate_limiter=rate_limiter)  # type: ignore[arg-type]

        assert job.failure_handled_at is not None
        assert job.failure_handled_at >= before

    async def test_never_calls_the_generation_client(self) -> None:
        """A4 (never auto-regenerates): `sweep_terminal_failures` isn't even handed a
        generation client -- there is no port to call. This test documents that
        contract (and would fail loudly with a TypeError if a future change added
        one without updating this guard)."""
        job = _failed_job()
        rate_limiter = _RecordingRateLimiter()

        await sweep_terminal_failures(job, rate_limiter=rate_limiter)  # type: ignore[arg-type]

        assert job.state == JOB_STATE_FAILED  # never transitioned to QUEUED/SUBMITTING


# ═══════════════════════════════════════════════════════════════════════════════════
# A1 idempotency: a re-claim of an already-handled FAILED row never double-acts.
# ═══════════════════════════════════════════════════════════════════════════════════


class TestSweepTerminalFailuresIdempotency:
    async def test_a_second_claim_after_the_stamp_finds_nothing_to_refund_or_notify(
        self, session: AsyncSession
    ) -> None:
        """The idempotency guarantee (D2) is row-claim-based, not a check inside
        `sweep_terminal_failures` itself: `claim_terminal_failure_job`'s `WHERE
        failure_handled_at IS NULL` is what makes a second sweep pass over the SAME
        row a no-op. Drives the real claim + sweep + commit twice over one job to
        prove that end to end -- exactly one refund, exactly one notification, ever,
        no matter how many watchdog ticks later a second sweep pass runs."""
        await _add(session, _job(job_id="failed-1", state=JOB_STATE_FAILED))
        rate_limiter = _RecordingRateLimiter()
        published: list[tuple[str, str]] = []

        async def _publish(user_id: str, message: str) -> None:
            published.append((user_id, message))

        # First sweep: claims the row, refunds, notifies, stamps `failure_handled_at`.
        job = await claim_terminal_failure_job(session)
        assert job is not None
        await sweep_terminal_failures(
            job, rate_limiter=rate_limiter, publish_user_event=_publish  # type: ignore[arg-type]
        )
        await session.commit()

        # Second sweep (a later watchdog tick, or a concurrent worker instance): the
        # claim query itself finds nothing left to hand back.
        second_claim = await claim_terminal_failure_job(session)

        assert second_claim is None
        assert len(rate_limiter.refund_calls) == 1  # never double-refunded
        assert len(published) == 1  # never double-notified


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
