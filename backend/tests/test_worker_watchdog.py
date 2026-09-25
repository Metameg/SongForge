"""`_run_sweeps` wiring (issue #16, phase 4 contract audit): one pass composes each of
the four `claim_*`/`sweep_*` pairs -- claim, sweep (if a row was claimed), commit -- in
its own short-lived session, mirroring `worker/dispatch.py::_drain_ready_jobs`'s
"one session per claimed row" convention (see `tests/test_worker_dispatch.py`).

This file closes a coverage gap flagged during the phase-4 data-contract audit:
`.orchestrator/plan-issue-16.md` step 5 called for a dedicated `_run_sweeps` test
(mirroring `test_worker_dispatch.py`), but no such file existed -- so nothing proved
that `_run_sweeps` actually calls each `claim_*`/`sweep_*` pair, threads the SAME
`notify` callable into both `sweep_waiting_overdue` (as `notify_ingest`) and
`sweep_submitting_stuck` (as `notify_new_job`), or skips the sweep+commit entirely
when a claim comes back empty. `run_watchdog` itself (the real asyncpg/Redis wiring
and its poll-backstop outer loop) is intentionally left untested, matching
`run_dispatch`/`run_ingest`'s established precedent -- resilience, not a dedicated
unit test, is its correctness property.
"""

from __future__ import annotations

import pytest

from songforge.config import Settings
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

    def __init__(self, commits: list[str], label: str) -> None:
        self._commits = commits
        self._label = label

    async def commit(self) -> None:
        self._commits.append(self._label)


class _NullSessionCtx:
    def __init__(self, commits: list[str], label: str) -> None:
        self._commits = commits
        self._label = label

    async def __aenter__(self) -> _NullSession:
        return _NullSession(self._commits, self._label)

    async def __aexit__(self, *exc: object) -> None:
        return None


def _sessionmaker_factory(commits: list[str]):  # type: ignore[no-untyped-def]
    """A fake `async_sessionmaker`: `_run_sweeps` opens FOUR separate sessions (one
    per sweep, in claim order), so each call is labelled by its ordinal to prove the
    "one short-lived session per sweep" shape rather than one shared session reused
    across all four."""
    labels = iter(["waiting", "submitting", "ingest", "terminal"])

    def _make() -> _NullSessionCtx:
        return _NullSessionCtx(commits, next(labels))

    return _make


class _FakeJob:
    def __init__(self, job_id: str = "job-1") -> None:
        self.job_id = job_id


async def test_run_sweeps_claims_sweeps_and_commits_all_four_pairs_when_rows_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every claim hands back a row, `_run_sweeps` must call the matching sweep
    exactly once per pair and commit each in its own session -- proving the wiring
    (not just the pure decision functions `tests/test_watchdog.py` already covers)
    actually threads claim -> sweep -> commit together."""
    calls: list[str] = []
    commits: list[str] = []

    async def _fake_claim_waiting(session: object, settings: Settings) -> _FakeJob:
        calls.append("claim_waiting")
        return _FakeJob("waiting-job")

    async def _fake_sweep_waiting(job: _FakeJob, **kwargs: object) -> None:
        calls.append("sweep_waiting")
        assert job.job_id == "waiting-job"
        assert kwargs["notify_ingest"] is not None

    async def _fake_claim_submitting(session: object, settings: Settings) -> _FakeJob:
        calls.append("claim_submitting")
        return _FakeJob("submitting-job")

    async def _fake_sweep_submitting(job: _FakeJob, **kwargs: object) -> None:
        calls.append("sweep_submitting")
        assert job.job_id == "submitting-job"
        assert kwargs["notify_new_job"] is not None

    async def _fake_claim_ingest(session: object, settings: Settings) -> _FakeJob:
        calls.append("claim_ingest")
        return _FakeJob("ingest-job")

    async def _fake_sweep_ingest(job: _FakeJob, **kwargs: object) -> None:
        calls.append("sweep_ingest")
        assert job.job_id == "ingest-job"

    async def _fake_claim_terminal(session: object) -> _FakeJob:
        calls.append("claim_terminal")
        return _FakeJob("terminal-job")

    async def _fake_sweep_terminal(job: _FakeJob, **kwargs: object) -> None:
        calls.append("sweep_terminal")
        assert job.job_id == "terminal-job"
        assert kwargs["rate_limiter"] is not None
        assert kwargs["publish_user_event"] is not None

    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_waiting_overdue_job", _fake_claim_waiting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_waiting_overdue", _fake_sweep_waiting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_submitting_stuck_job", _fake_claim_submitting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_submitting_stuck", _fake_sweep_submitting
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_ingest_overdue_job", _fake_claim_ingest
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_ingest_overdue", _fake_sweep_ingest
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.claim_terminal_failure_job", _fake_claim_terminal
    )
    monkeypatch.setattr(
        "songforge.worker.watchdog.sweep_terminal_failures", _fake_sweep_terminal
    )

    async def _notify(channel: str, payload: str) -> None:
        return None

    async def _publish_user_event(user_id: str, message: str) -> None:
        return None

    await _run_sweeps(
        _sessionmaker_factory(commits),
        object(),  # client
        object(),  # rate_limiter
        _settings(),
        _notify,
        _publish_user_event,
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
    # One commit per pair, each in its OWN session (not one shared commit at the end).
    assert commits == ["waiting", "submitting", "ingest", "terminal"]


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
