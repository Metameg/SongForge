"""`_run_sweeps` wiring (issue #16, phase 4 contract audit + phase 5 fix pass F2/F4).

Mirrors `test_worker_dispatch.py`: one pass composes each of the four `claim_*`/
`sweep_*` pairs -- claim, sweep (if a row was claimed), commit -- in its own
short-lived session, mirroring `worker/dispatch.py::_drain_ready_jobs`'s "one session
per claimed row" convention.

Two contract changes from the original phase-4 coverage, both landed by the phase-5
fix pass:

- F2: each of the four sweeps now DRAINS TO EXHAUSTION -- claim, sweep, commit,
  repeat, until that sweep type's claim returns `None` -- rather than acting on at
  most one row per type per tick. `test_run_sweeps_drains_each_sweep_type_to_exhaustion`
  replaces the old pinning test that documented (as a known concern, not a defect) the
  one-row-per-type-per-tick shape; that shape no longer exists.
- F4: the `sweep_*` decision functions no longer call NOTIFY/refund/publish
  themselves -- they return an intent (a `NotifyIntent` tuple, or a
  `TerminalFailureIntent`), and `_run_sweeps`' `_drain_*` helpers fire the
  corresponding best-effort side effect (`_safe_notify`/`_safe_refund`/
  `_safe_publish_user_event`) only AFTER that row's own `session.commit()` succeeds.
  `test_run_sweeps_drains_each_pair_and_fires_side_effects_after_commit` proves both
  the ordering (commit happens before the spy sees the intent fired) and that each
  sweep's specific side effect is threaded to the right hook.

Phase-5 RE-REVIEW fix: F2's drain-to-exhaustion loop combined with F1's "leave the
job WAITING untouched on a still-IN_QUEUE poll or a `/byId` exception" behavior was a
non-termination bug -- `_drain_waiting_overdue`'s `while True` re-claimed the SAME
unmutated row forever within one tick, since nothing about it ever changes
(`updated_at` is deliberately not bumped). The `TestDrainWaitingOverdueTermination`
class below drives the REAL `claim_waiting_overdue_job` (not a monkeypatched one, the
gap that let the bug through review) against a real in-memory SQLite session to prove
the fix (`claim_waiting_overdue_job`'s `exclude_job_ids` param, accumulated per-tick
by `_drain_waiting_overdue`) actually terminates for both the still-pending and the
poll-failure cases, while still draining a batch of genuinely-recoverable rows fully.

`run_watchdog` itself (the real asyncpg/Redis wiring and its poll-backstop outer loop)
is intentionally left untested, matching `run_dispatch`/`run_ingest`'s established
precedent -- resilience, not a dedicated unit test, is its correctness property.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_IN_QUEUE,
    GenerationStatus,
    GenerationTransientError,
)
from songforge.models import (
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
)
from songforge.web.rate_limit import Identity
from songforge.worker.watchdog import _drain_waiting_overdue, _run_sweeps


def _settings() -> Settings:
    return Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "MUSICGPT_BASE_URL": "http://musicgpt.test",
            "MUSICGPT_API_KEY": "test-key",
        }
    )


class _NullSession:
    """No real DB work happens in these tests -- every `claim_*`/`sweep_*` is
    monkeypatched -- so this only needs to satisfy `session.commit()`."""

    def __init__(self, commits: list[str]) -> None:
        self._commits = commits

    async def commit(self) -> None:
        self._commits.append("commit")


class _NullSessionCtx:
    def __init__(self, commits: list[str]) -> None:
        self._commits = commits

    async def __aenter__(self) -> _NullSession:
        return _NullSession(self._commits)

    async def __aexit__(self, *exc: object) -> None:
        return None


def _sessionmaker_factory(commits: list[str]):  # type: ignore[no-untyped-def]
    """A fake `async_sessionmaker`: every claim attempt (including an "empty" one that
    finds no claimable row and never commits) opens its own fresh session, matching
    drain-to-exhaustion's "one session per claim attempt" shape."""

    def _make() -> _NullSessionCtx:
        return _NullSessionCtx(commits)

    return _make


class _FakeJob:
    def __init__(self, job_id: str = "job-1") -> None:
        self.job_id = job_id


class _FakeTerminalIntent:
    """Stands in for `jobs.watchdog.TerminalFailureIntent` -- only the attributes
    `_drain_terminal_failures` actually reads."""

    def __init__(self, job_id: str) -> None:
        self.identity = Identity(user_id="user-1", is_authenticated=False, minted=False)
        self.ip = "203.0.113.5"
        self.day = "2026-09-25"
        self.user_id = "user-1"
        self.job_id = job_id


def _once_then_none(label: str, calls: list[str], job_id: str):  # type: ignore[no-untyped-def]
    """A fake `claim_*` that hands back exactly one job, then `None` forever after --
    the minimal shape that exercises a drain loop's "claim, act, claim again, find
    nothing, stop" cycle without looping forever."""
    claimed = {"done": False}

    async def _claim(*_args: object, **_kwargs: object) -> _FakeJob | None:
        if claimed["done"]:
            return None
        claimed["done"] = True
        calls.append(label)
        return _FakeJob(job_id)

    return _claim


async def test_run_sweeps_drains_each_pair_and_fires_side_effects_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every claim hands back exactly one row, `_run_sweeps` must claim, sweep,
    and commit each of the four pairs in order (F2's drain loop still stops once a
    type is exhausted), and fire each pair's best-effort side effect -- the ingest/
    new-job NOTIFY, the refund, the per-user publish -- ONLY from the returned intent,
    AFTER that row's commit (F4)."""
    calls: list[str] = []
    commits: list[str] = []
    notified: list[tuple[str, str]] = []
    refunded: list[tuple[Identity, str, str]] = []
    published: list[tuple[str, str]] = []

    async def _fake_sweep_waiting(job: _FakeJob, **kwargs: object) -> tuple[str, str]:
        calls.append("sweep_waiting")
        assert job.job_id == "waiting-job"
        return ("ingest-channel", job.job_id)

    async def _fake_sweep_submitting(job: _FakeJob, **kwargs: object) -> tuple[str, str]:
        calls.append("sweep_submitting")
        assert job.job_id == "submitting-job"
        return ("new-job-channel", job.job_id)

    async def _fake_sweep_ingest(job: _FakeJob, **kwargs: object) -> None:
        calls.append("sweep_ingest")
        assert job.job_id == "ingest-job"

    async def _fake_sweep_terminal(job: _FakeJob) -> _FakeTerminalIntent:
        calls.append("sweep_terminal")
        assert job.job_id == "terminal-job"
        return _FakeTerminalIntent(job.job_id)

    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_waiting_overdue_job",
        _once_then_none("claim_waiting", calls, "waiting-job"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_waiting_overdue", _fake_sweep_waiting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_submitting_stuck_job",
        _once_then_none("claim_submitting", calls, "submitting-job"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_submitting_stuck", _fake_sweep_submitting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_ingest_overdue_job",
        _once_then_none("claim_ingest", calls, "ingest-job"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_ingest_overdue", _fake_sweep_ingest
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_terminal_failure_job",
        _once_then_none("claim_terminal", calls, "terminal-job"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_terminal_failures", _fake_sweep_terminal
    )

    async def _notify(channel: str, payload: str) -> None:
        notified.append((channel, payload))

    async def _safe_refund_spy(
        rate_limiter: object, identity: Identity, ip: str, day: str
    ) -> None:
        refunded.append((identity, ip, day))

    async def _safe_publish_spy(
        publish: object, user_id: str, message: str
    ) -> None:
        published.append((user_id, message))

    monkeypatch.setattr("songforge.worker.watchdog._safe_refund", _safe_refund_spy)
    monkeypatch.setattr(
        "songforge.worker.watchdog._safe_publish_user_event", _safe_publish_spy
    )

    await _run_sweeps(
        _sessionmaker_factory(commits),
        object(),  # client
        object(),  # rate_limiter
        _settings(),
        _notify,
        object(),  # publish_user_event
    )

    assert calls == [
        "claim_waiting",
        "sweep_waiting",
        "claim_submitting",
        "sweep_submitting",
        "claim_ingest",
        "sweep_ingest",
        "claim_terminal",
        "sweep_terminal",
    ]
    # One commit per handled row. Each drain loop also makes one further "empty"
    # claim attempt (proving it looked for more before moving on) that finds nothing
    # and never commits -- so this is 4 commits, not fewer and not one per claim call.
    assert commits == ["commit", "commit", "commit", "commit"]
    assert notified == [
        ("ingest-channel", "waiting-job"),
        ("new-job-channel", "submitting-job"),
    ]
    assert refunded == [
        (
            Identity(user_id="user-1", is_authenticated=False, minted=False),
            "203.0.113.5",
            "2026-09-25",
        )
    ]
    assert published == [
        ("user-1", '{"event": "job-failed", "job_id": "terminal-job"}')
    ]


async def test_run_sweeps_skips_sweep_and_commit_when_nothing_is_claimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pair whose claim returns `None` must neither call its sweep nor commit --
    an idle tick over all-fresh rows must be a true no-op for every pair."""
    calls: list[str] = []
    commits: list[str] = []

    async def _fake_claim_none_two_args(
        session: object, settings: Settings, **_kwargs: object
    ) -> None:
        return None

    async def _fake_claim_none_one_arg(session: object) -> None:
        return None

    def _unexpected_sweep(name: str):  # type: ignore[no-untyped-def]
        async def _sweep(job: object, **kwargs: object) -> None:
            calls.append(name)
            raise AssertionError(f"{name} must never run when nothing was claimed")

        return _sweep

    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_waiting_overdue_job", _fake_claim_none_two_args
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_waiting_overdue",
        _unexpected_sweep("sweep_waiting"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_submitting_stuck_job",
        _fake_claim_none_two_args,
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_submitting_stuck",
        _unexpected_sweep("sweep_submitting"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_ingest_overdue_job", _fake_claim_none_two_args
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_ingest_overdue",
        _unexpected_sweep("sweep_ingest"),
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_terminal_failure_job",
        _fake_claim_none_one_arg,
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_terminal_failures",
        _unexpected_sweep("sweep_terminal"),
    )

    async def _notify(channel: str, payload: str) -> None:
        return None

    async def _publish_user_event(user_id: str, message: str) -> None:
        return None

    await _run_sweeps(
        _sessionmaker_factory(commits),
        object(),
        object(),
        _settings(),
        _notify,
        _publish_user_event,
    )

    assert calls == []
    assert commits == []


async def test_run_sweeps_drains_each_sweep_type_to_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: replaces the old pinning test (which documented, as a known concern rather
    than a defect, that `_run_sweeps` claimed AT MOST ONE row per sweep type per
    call). That shape is gone -- each sweep type now drains fully, mirroring
    `worker/dispatch.py::_drain_ready_jobs` / `worker/ingest.py::
    _drain_ingest_pending`'s "claim, act, commit, repeat until the claim comes back
    empty" loop. This fake hands back THREE overdue `WAITING_FOR_WEBHOOK` rows in a
    row before finally returning `None`; all three must be claimed and swept in the
    SAME `_run_sweeps` call, not just the first."""
    claim_calls = 0
    sweep_calls = 0

    async def _fake_claim_waiting(
        session: object, settings: Settings, **_kwargs: object
    ) -> _FakeJob | None:
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls > 3:
            return None
        return _FakeJob(f"waiting-{claim_calls}")

    async def _fake_sweep_waiting(job: _FakeJob, **kwargs: object) -> None:
        nonlocal sweep_calls
        sweep_calls += 1
        return None

    async def _fake_claim_none_two_args(
        session: object, settings: Settings, **_kwargs: object
    ) -> None:
        return None

    async def _fake_claim_none_one_arg(session: object) -> None:
        return None

    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_waiting_overdue_job", _fake_claim_waiting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_waiting_overdue", _fake_sweep_waiting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_submitting_stuck_job", _fake_claim_none_two_args
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_ingest_overdue_job", _fake_claim_none_two_args
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_terminal_failure_job", _fake_claim_none_one_arg
    )

    async def _notify(channel: str, payload: str) -> None:
        return None

    async def _publish_user_event(user_id: str, message: str) -> None:
        return None

    commits: list[str] = []
    await _run_sweeps(
        _sessionmaker_factory(commits),
        object(),
        object(),
        _settings(),
        _notify,
        _publish_user_event,
    )

    # 3 rows claimed and swept, plus one final empty claim that stops the drain.
    assert claim_calls == 4
    assert sweep_calls == 3
    assert commits == ["commit", "commit", "commit"]


# ═══════════════════════════════════════════════════════════════════════════════════
# Non-termination-bug fix (phase 5 re-review): `_drain_waiting_overdue` against a REAL
# `claim_waiting_overdue_job` + in-memory SQLite session -- the monkeypatched-claim
# tests above can't catch this class of bug because a fake claim function that returns
# `None` after N calls can never reproduce "the real claim query keeps re-selecting
# the same unmutated row forever." These tests seed genuine `Job` rows and drive the
# real claim/sweep/commit loop end to end.
# ═══════════════════════════════════════════════════════════════════════════════════

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
async def real_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A genuine in-memory SQLite engine + sessionmaker (unlike `_sessionmaker_factory`
    above, which never touches a real database) -- `_drain_waiting_overdue` opens
    several sessions off this across one call, exactly like it would against the real
    app-wide sessionmaker in production."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_waiting_job(**overrides: object) -> Job:
    """A genuinely-overdue `WAITING_FOR_WEBHOOK` row (mirrors `tests/test_watchdog.py::
    _waiting_job`): `eta=60` + the default `watchdog_waiting_overdue_buffer_seconds`
    is comfortably exceeded by `updated_at` being 600s in the past."""
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
        updated_at=_now() - timedelta(seconds=600),
        client_ip="203.0.113.5",
        is_authenticated=False,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


class _AlwaysStatusClient:
    """File-local fake `GenerationClient`: `get_status_by_id` always returns (or
    raises) the SAME fixed outcome, no matter which/how many `task_id`s are polled --
    exactly the "nothing about this row will ever change" scenario the non-termination
    bug needed. Counts calls so a test can assert a bound on how many times it was
    actually invoked (proving "polled once per tick", not "never hammered")."""

    def __init__(self, outcome: GenerationStatus | Exception) -> None:
        self._outcome = outcome
        self.calls = 0

    async def create(self, **kwargs: object) -> None:
        raise AssertionError("the watchdog must never call create() -- no re-charging")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("sweep_waiting_overdue must use get_status_by_id, not this")

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class TestDrainWaitingOverdueTermination:
    async def test_terminates_when_a_row_stays_in_queue(
        self, real_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The bug: a still-IN_QUEUE row is left WAITING untouched by
        `sweep_waiting_overdue` (no state change, `updated_at` not bumped), so the
        real claim query would re-select it on every loop iteration forever without
        the `exclude_job_ids` fix. Bounded by `asyncio.wait_for` so a regression fails
        the test on a timeout rather than hanging the suite; the call is also
        expected to complete well within that bound when the fix is in place."""
        async with real_sessionmaker() as session:
            session.add(_seed_waiting_job(job_id="stuck-in-queue"))
            await session.commit()

        client = _AlwaysStatusClient(
            GenerationStatus(
                status=GENERATION_STATUS_IN_QUEUE, audio_url=None, duration=None, title=None
            )
        )
        settings = _settings()

        async def _notify(channel: str, payload: str) -> None:
            return None

        await asyncio.wait_for(
            _drain_waiting_overdue(real_sessionmaker, client, settings, _notify),
            timeout=5.0,
        )

        # Polled exactly once THIS tick -- not hammered, not skipped.
        assert client.calls == 1

        async with real_sessionmaker() as session:
            row = await session.get(Job, "stuck-in-queue")
            assert row is not None
            assert row.state == JOB_STATE_WAITING_FOR_WEBHOOK  # still untouched

    async def test_terminates_when_the_byid_poll_keeps_failing(
        self, real_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """F1's exception-safety path has the exact same non-termination exposure as
        the IN_QUEUE case: the job is left WAITING untouched on every poll attempt."""
        async with real_sessionmaker() as session:
            session.add(_seed_waiting_job(job_id="stuck-erroring"))
            await session.commit()

        client = _AlwaysStatusClient(GenerationTransientError("byId returned 503"))
        settings = _settings()

        async def _notify(channel: str, payload: str) -> None:
            return None

        await asyncio.wait_for(
            _drain_waiting_overdue(real_sessionmaker, client, settings, _notify),
            timeout=5.0,
        )

        assert client.calls == 1

        async with real_sessionmaker() as session:
            row = await session.get(Job, "stuck-erroring")
            assert row is not None
            assert row.state == JOB_STATE_WAITING_FOR_WEBHOOK

    async def test_still_drains_multiple_genuinely_recoverable_rows_to_exhaustion(
        self, real_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The fix must not regress F2: rows that actually transition (COMPLETED
        here) keep draining fully in one call -- the exclusion set only matters for
        rows that DON'T transition."""
        async with real_sessionmaker() as session:
            for i in range(3):
                session.add(
                    _seed_waiting_job(
                        job_id=f"recoverable-{i}",
                        task_id=f"task-{i}",
                        updated_at=_now() - timedelta(seconds=600 + i),
                    )
                )
            await session.commit()

        client = _AlwaysStatusClient(
            GenerationStatus(
                status=GENERATION_STATUS_COMPLETED,
                audio_url="http://musicgpt.test/audio/recovered",
                duration=180.0,
                title="Recovered",
            )
        )
        settings = _settings()
        notified: list[tuple[str, str]] = []

        async def _notify(channel: str, payload: str) -> None:
            notified.append((channel, payload))

        await asyncio.wait_for(
            _drain_waiting_overdue(real_sessionmaker, client, settings, _notify),
            timeout=5.0,
        )

        assert client.calls == 3  # all three rows polled, none skipped
        assert len(notified) == 3  # one ingest-wake NOTIFY per recovered row

        async with real_sessionmaker() as session:
            for i in range(3):
                row = await session.get(Job, f"recoverable-{i}")
                assert row is not None
                assert row.state == JOB_STATE_INGEST_PENDING

    async def test_terminates_with_a_mix_of_stuck_and_recoverable_rows(
        self, real_sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The realistic mixed scenario: one row will never transition this tick
        (IN_QUEUE), another genuinely recovers (COMPLETED). Both distinct `task_id`s
        are routed to the SAME fake client instance, which branches on `task_id` --
        proving the exclusion set lets the drain move PAST a stuck row to reach a
        later, genuinely-claimable one, rather than either looping forever on the
        stuck row or stopping early and never reaching the recoverable one."""

        class _ByTaskIdClient:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def create(self, **kwargs: object) -> None:
                raise AssertionError("must never re-charge")

            async def get_audio_url_by_id(self, task_id: str) -> str:
                raise AssertionError("must use get_status_by_id")

            async def get_status_by_id(self, task_id: str) -> GenerationStatus:
                self.calls.append(task_id)
                if task_id == "task-stuck":
                    return GenerationStatus(
                        status=GENERATION_STATUS_IN_QUEUE,
                        audio_url=None,
                        duration=None,
                        title=None,
                    )
                return GenerationStatus(
                    status=GENERATION_STATUS_COMPLETED,
                    audio_url="http://musicgpt.test/audio/recovered",
                    duration=180.0,
                    title="Recovered",
                )

        async with real_sessionmaker() as session:
            # Oldest first: the stuck row would be re-claimed first on every
            # iteration if the exclusion set didn't move it out of the way.
            session.add(
                _seed_waiting_job(
                    job_id="stuck",
                    task_id="task-stuck",
                    updated_at=_now() - timedelta(seconds=700),
                )
            )
            session.add(
                _seed_waiting_job(
                    job_id="recoverable",
                    task_id="task-recoverable",
                    updated_at=_now() - timedelta(seconds=600),
                )
            )
            await session.commit()

        client = _ByTaskIdClient()
        settings = _settings()

        async def _notify(channel: str, payload: str) -> None:
            return None

        await asyncio.wait_for(
            _drain_waiting_overdue(real_sessionmaker, client, settings, _notify),
            timeout=5.0,
        )

        # Each row polled exactly once -- the stuck row wasn't hammered, and the
        # recoverable row wasn't starved by it.
        assert client.calls.count("task-stuck") == 1
        assert client.calls.count("task-recoverable") == 1

        async with real_sessionmaker() as session:
            stuck = await session.get(Job, "stuck")
            recovered = await session.get(Job, "recoverable")
            assert stuck is not None
            assert stuck.state == JOB_STATE_WAITING_FOR_WEBHOOK
            assert recovered is not None
            assert recovered.state == JOB_STATE_INGEST_PENDING
