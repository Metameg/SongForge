"""`_drain_ingest_pending` loop behavior (issue #13): call `claim_next_ingest_job` +
`ingest_claimed_job` repeatedly, one short-lived session per job, until nothing is
claimable. Mirrors `tests/test_worker_dispatch.py`. The real LISTEN/NOTIFY wiring in
`run_ingest` is thin I/O (mirrors `worker/dispatch.py`'s precedent: "has no dedicated
unit test -- resilience is its main correctness property") and isn't unit-tested here
beyond its poll backstop.
"""

from __future__ import annotations

import asyncio

import pytest

import songforge.worker.ingest as worker_ingest_module
from songforge.config import Settings
from songforge.worker.ingest import _drain_ingest_pending, run_ingest


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
    """No real DB work happens in these tests -- claim_next_ingest_job/
    ingest_claimed_job are monkeypatched -- so this only needs to satisfy
    `session.commit()`."""

    async def commit(self) -> None:
        return None


class _NullSessionCtx:
    async def __aenter__(self) -> _NullSession:
        return _NullSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _sessionmaker():  # type: ignore[no-untyped-def]
    return _NullSessionCtx()


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


async def test_drain_calls_ingest_until_claim_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", "job-2", None])
    claim_calls = 0
    ingest_calls = 0

    async def _fake_claim(session: object) -> _FakeJob | None:
        nonlocal claim_calls
        claim_calls += 1
        job_id = next(remaining)
        return None if job_id is None else _FakeJob(job_id)

    async def _fake_ingest(job: _FakeJob, **kwargs: object) -> None:
        nonlocal ingest_calls
        ingest_calls += 1

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    await _drain_ingest_pending(
        _sessionmaker, object(), object(), object(), _settings()  # type: ignore[arg-type]
    )

    assert claim_calls == 3  # job-1, job-2, then None -> stop
    assert ingest_calls == 2


async def test_drain_returns_immediately_when_nothing_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_calls = 0

    async def _fake_claim(session: object) -> None:
        return None

    async def _fake_ingest(job: object, **kwargs: object) -> None:
        nonlocal ingest_calls
        ingest_calls += 1

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    await _drain_ingest_pending(
        _sessionmaker, object(), object(), object(), _settings()  # type: ignore[arg-type]
    )

    assert ingest_calls == 0


# ── `run_ingest`'s poll backstop (mirrors `test_worker_dispatch.py`'s equivalent) ──


class _FakeAsyncpgConnection:
    """Stands in for the real LISTEN/NOTIFY connection. `add_listener` deliberately
    never calls back -- simulating a NOTIFY that was missed/never sent -- so the test
    can prove the backstop poll alone still drives re-draining."""

    async def add_listener(self, channel: str, callback: object) -> None:
        return None

    async def close(self) -> None:
        return None


async def test_run_ingest_poll_backstop_redrains_when_no_notify_ever_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_connect(dsn: str) -> _FakeAsyncpgConnection:
        return _FakeAsyncpgConnection()

    monkeypatch.setattr(worker_ingest_module.asyncpg, "connect", _fake_connect)

    drain_calls = 0
    stop = asyncio.Event()

    async def _fake_drain(
        sessionmaker: object, storage: object, generation_client: object,
        downloader: object, settings: Settings,
    ) -> None:
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls >= 3:
            stop.set()

    monkeypatch.setattr(worker_ingest_module, "_drain_ingest_pending", _fake_drain)

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "INGEST_POLL_BACKSTOP_SECONDS": "0.01",
        }
    )

    await asyncio.wait_for(run_ingest(settings, stop), timeout=5.0)

    # Three drain passes happened purely from the poll-backstop timeout ticking --
    # `_FakeAsyncpgConnection.add_listener` never invoked a wake callback, so nothing
    # but the backstop could have driven passes 2 and 3.
    assert drain_calls == 3
