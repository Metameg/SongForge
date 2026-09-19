"""`_drain_ready_jobs` loop behavior (issue #12, criterion #5's drain half): call
`claim_next_job` + `dispatch_claimed_job` repeatedly, one short-lived session per job,
until nothing is claimable or the semaphore denies a slot. The real LISTEN/NOTIFY
wiring in `run_dispatch` is thin I/O (mirrors `worker/radio_coordinator.py`'s
precedent: "has no dedicated unit test -- resilience is its main correctness
property") and isn't unit-tested here.
"""

from __future__ import annotations

import asyncio

import pytest

import songforge.worker.dispatch as worker_dispatch_module
from songforge.config import Settings
from songforge.worker.dispatch import _drain_ready_jobs, run_dispatch


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


# ── `run_dispatch`'s poll backstop (criterion #5: NOTIFY wakes dispatch, but a slow
# poll is still a backstop for a missed notification) ──────────────────────────────
#
# The rest of this file drives `_drain_ready_jobs` directly (the testable decision
# step). This exercises `run_dispatch`'s own outer loop -- normally left untested per
# this repo's convention (mirrors `worker/radio_coordinator.py`: "any error inside it
# is logged and backed off, never raised... resilience is its main correctness
# property") -- specifically for the one behaviour that convention doesn't cover: that
# the loop re-drains on a plain timeout even when NO wake-up (`add_listener` callback)
# ever fires, i.e. the poll backstop genuinely backstops a missed/never-sent NOTIFY.
# The real `asyncpg`/LISTEN connection and `httpx.AsyncClient` are never used for real
# network I/O -- `asyncpg.connect` is monkeypatched to a fake connection whose
# `add_listener` never invokes its callback, so `wake` never fires and only the
# `dispatch_poll_backstop_seconds` timeout can drive further drain passes.


class _FakeAsyncpgConnection:
    """Stands in for the real LISTEN/NOTIFY connection. `add_listener` deliberately
    never calls back -- simulating a NOTIFY that was missed/never sent -- so the test
    can prove the backstop poll alone still drives re-draining."""

    async def add_listener(self, channel: str, callback: object) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_run_dispatch_poll_backstop_redrains_when_no_notify_ever_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_connect(dsn: str) -> _FakeAsyncpgConnection:
        return _FakeAsyncpgConnection()

    monkeypatch.setattr(worker_dispatch_module.asyncpg, "connect", _fake_connect)

    drain_calls = 0
    stop = asyncio.Event()

    async def _fake_drain(sessionmaker: object, semaphore: object, client: object,
                           settings: Settings, notify_release: object) -> None:
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls >= 3:
            stop.set()

    monkeypatch.setattr(worker_dispatch_module, "_drain_ready_jobs", _fake_drain)

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "DISPATCH_POLL_BACKSTOP_SECONDS": "0.01",
        }
    )

    await asyncio.wait_for(run_dispatch(settings, stop), timeout=5.0)

    # Three drain passes happened purely from the poll-backstop timeout ticking --
    # `_FakeAsyncpgConnection.add_listener` never invoked a wake callback, so nothing
    # but the backstop could have driven passes 2 and 3.
    assert drain_calls == 3
