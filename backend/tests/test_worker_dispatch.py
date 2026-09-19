"""`_drain_ready_jobs` loop behavior (issue #12, criterion #5's drain half): call
`claim_next_job` + `dispatch_claimed_job` repeatedly, one short-lived session per job,
until nothing is claimable or the semaphore denies a slot. The real LISTEN/NOTIFY
wiring in `run_dispatch` is thin I/O (mirrors `worker/radio_coordinator.py`'s
precedent: "has no dedicated unit test -- resilience is its main correctness
property") and isn't unit-tested here.
"""

from __future__ import annotations

import pytest

from songforge.config import Settings
from songforge.worker.dispatch import _drain_ready_jobs


def _settings() -> Settings:
    return Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )


class _NullSession:
    """No real DB work happens in these tests -- claim_next_job/dispatch_claimed_job
    are monkeypatched -- so this only needs to satisfy `session.commit()`."""

    async def commit(self) -> None:
        return None


class _NullSessionCtx:
    async def __aenter__(self) -> _NullSession:
        return _NullSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _sessionmaker():  # type: ignore[no-untyped-def]
    return _NullSessionCtx()


async def _noop_notify(job_id: str) -> None:
    return None


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


async def test_drain_calls_dispatch_until_claim_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", "job-2", None])
    claim_calls = 0
    dispatch_calls = 0

    async def _fake_claim(session: object) -> _FakeJob | None:
        nonlocal claim_calls
        claim_calls += 1
        job_id = next(remaining)
        return None if job_id is None else _FakeJob(job_id)

    async def _fake_dispatch(job: _FakeJob, **kwargs: object) -> bool:
        nonlocal dispatch_calls
        dispatch_calls += 1
        return True  # a slot was acquired and the API was called either way

    monkeypatch.setattr("songforge.worker.dispatch.claim_next_job", _fake_claim)
    monkeypatch.setattr("songforge.worker.dispatch.dispatch_claimed_job", _fake_dispatch)

    await _drain_ready_jobs(
        _sessionmaker, object(), object(), _settings(), _noop_notify  # type: ignore[arg-type]
    )

    assert claim_calls == 3  # job-1, job-2, then None -> stop
    assert dispatch_calls == 2


async def test_drain_stops_immediately_when_no_slot_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dispatch_claimed_job` returning False means no slot was available (criterion
    #3: never call the API without a slot) -- the drain loop must not keep re-claiming
    into the same contention; it stops and waits for the next wake-up."""
    claim_calls = 0

    async def _fake_claim(session: object) -> _FakeJob:
        nonlocal claim_calls
        claim_calls += 1
        return _FakeJob("job-1")

    async def _fake_dispatch(job: _FakeJob, **kwargs: object) -> bool:
        return False  # no slot

    monkeypatch.setattr("songforge.worker.dispatch.claim_next_job", _fake_claim)
    monkeypatch.setattr("songforge.worker.dispatch.dispatch_claimed_job", _fake_dispatch)

    await _drain_ready_jobs(
        _sessionmaker, object(), object(), _settings(), _noop_notify  # type: ignore[arg-type]
    )

    assert claim_calls == 1


async def test_drain_returns_immediately_when_nothing_is_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_calls = 0

    async def _fake_claim(session: object) -> None:
        return None

    async def _fake_dispatch(job: object, **kwargs: object) -> bool:
        nonlocal dispatch_calls
        dispatch_calls += 1
        return True

    monkeypatch.setattr("songforge.worker.dispatch.claim_next_job", _fake_claim)
    monkeypatch.setattr("songforge.worker.dispatch.dispatch_claimed_job", _fake_dispatch)

    await _drain_ready_jobs(
        _sessionmaker, object(), object(), _settings(), _noop_notify  # type: ignore[arg-type]
    )

    assert dispatch_calls == 0
