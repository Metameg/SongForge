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

`run_watchdog` itself (the real asyncpg/Redis wiring and its poll-backstop outer loop)
is intentionally left untested, matching `run_dispatch`/`run_ingest`'s established
precedent -- resilience, not a dedicated unit test, is its correctness property.
"""

from __future__ import annotations

import pytest

from songforge.config import Settings
from songforge.web.rate_limit import Identity
from songforge.worker.watchdog import _run_sweeps


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

    async def _claim(*_args: object) -> _FakeJob | None:
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

    async def _fake_claim_none_two_args(session: object, settings: Settings) -> None:
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

    async def _fake_claim_waiting(session: object, settings: Settings) -> _FakeJob | None:
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls > 3:
            return None
        return _FakeJob(f"waiting-{claim_calls}")

    async def _fake_sweep_waiting(job: _FakeJob, **kwargs: object) -> None:
        nonlocal sweep_calls
        sweep_calls += 1
        return None

    async def _fake_claim_none_two_args(session: object, settings: Settings) -> None:
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
