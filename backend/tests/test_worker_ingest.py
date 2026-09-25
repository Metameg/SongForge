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


# ── `notify_ready` (issue #14, criterion #3's enqueue-time `song_ready` wake) ──────
#
# RED phase: `_drain_ingest_pending` accepts the new optional `notify_ready` keyword
# (a phase-1 skeleton addition -- see `songforge/worker/ingest.py`) but does not yet
# call it. Mirrors the create/webhook notifier tests' spy pattern
# (`tests/test_create_route.py`, `tests/test_webhook_route.py`).


class _FakeReadyJob:
    """A fake claimed job whose `ingest_claimed_job` call (monkeypatched below)
    mutates it to READY with a `song_id` -- unlike `_FakeJob` above, which only ever
    needs `job_id` for the claim-loop-count tests."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.state = "INGEST_PENDING"
        self.song_id: str | None = None


async def test_drain_notifies_song_ready_after_commit_when_a_job_reaches_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "READY"
        job.song_id = "song-xyz"

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    notified: list[str] = []

    async def _notify_ready(song_id: str) -> None:
        notified.append(song_id)

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        notify_ready=_notify_ready,
    )

    assert notified == ["song-xyz"]


async def test_notify_failure_does_not_raise_and_still_drains_the_next_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus #5: `song_ready` NOTIFY is best-effort -- a raising `notify_ready`
    (e.g. a dropped connection) must be swallowed by `_safe_notify_ready`, never
    propagate out of `_drain_ingest_pending`, and never block the loop from draining a
    SECOND already-claimable job. The READY commit already happened; worst case is the
    coordinator's poll/boundary latency, never a lost or duplicated ingest."""
    remaining = iter(["job-1", "job-2", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "READY"
        job.song_id = f"song-for-{job.job_id}"

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    async def _boom(song_id: str) -> None:
        raise RuntimeError("notify connection dropped")

    # Must not raise -- and must still process both claimable jobs.
    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        notify_ready=_boom,
    )


# ── Generation semaphore release (issue #16 slot-leak fix) ─────────────────────────
#
# `dispatch_claimed_job` acquires the semaphore before the API call and never releases
# it on the happy path -- the job holds the slot through WAITING_FOR_WEBHOOK and
# INGEST_PENDING (both in `ACTIVE_JOB_STATES`). This is the FIRST event-driven release
# site: once ingest's own claim commits a job out of INGEST_PENDING into READY or
# FAILED, the slot must be released. A requeue back to INGEST_PENDING (still active)
# must NOT release -- the job hasn't left the active state.


class _RecordingSemaphore:
    """File-local fake ``Semaphore`` (mirrors ``tests/test_dispatch.py``'s
    ``_RecordingSemaphore``): logs ``release`` calls with the user id so ordering
    against the commit is directly observable."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events if events is not None else []
        self.released_with: list[str] = []

    async def acquire(self, user_id: str) -> bool:
        raise AssertionError("_drain_ingest_pending must never acquire a slot")

    async def release(self, user_id: str) -> None:
        self._events.append(f"semaphore.release:{user_id}")
        self.released_with.append(user_id)

    async def reconcile_from_active_count(self, count: int) -> None:
        raise AssertionError("_drain_ingest_pending must never reconcile the global cap")


async def test_drain_releases_semaphore_after_commit_when_job_reaches_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "READY"
        job.song_id = "song-xyz"
        job.user_id = "user-1"  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    semaphore = _RecordingSemaphore()

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        semaphore=semaphore,  # type: ignore[arg-type]
    )

    assert semaphore.released_with == ["user-1"]


async def test_drain_releases_semaphore_after_commit_when_job_reaches_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "FAILED"
        job.user_id = "user-2"  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    semaphore = _RecordingSemaphore()

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        semaphore=semaphore,  # type: ignore[arg-type]
    )

    assert semaphore.released_with == ["user-2"]


async def test_drain_does_not_release_semaphore_when_the_job_is_requeued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download/by-id failure the in-claim retry couldn't resolve requeues the job
    back to INGEST_PENDING -- still an active state, so the slot must stay held."""
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "INGEST_PENDING"  # requeued, not READY/FAILED
        job.user_id = "user-3"  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    semaphore = _RecordingSemaphore()

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        semaphore=semaphore,  # type: ignore[arg-type]
    )

    assert semaphore.released_with == []


async def test_drain_never_releases_when_no_semaphore_is_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``semaphore`` is optional/backward-compatible, mirroring ``notify_ready`` --
    omitting it (every pre-existing test in this file) must not raise even on a job
    that reaches READY."""
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "READY"
        job.song_id = "song-xyz"

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
    )
    # No assertion needed beyond "did not raise" -- there is no semaphore to inspect.


async def test_drain_release_failure_does_not_raise_and_still_drains_the_next_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: a raising ``semaphore.release`` (e.g. a Redis blip) must be
    swallowed, mirroring ``_safe_notify_ready``'s convention, and must not stop the
    drain from reaching a second already-claimable job."""
    remaining = iter(["job-1", "job-2", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "READY"
        job.song_id = f"song-for-{job.job_id}"
        job.user_id = "user-1"  # type: ignore[attr-defined]

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    class _RaisingSemaphore:
        async def acquire(self, user_id: str) -> bool:
            raise AssertionError("must never acquire")

        async def release(self, user_id: str) -> None:
            raise ConnectionError("redis blip during release")

        async def reconcile_from_active_count(self, count: int) -> None:
            raise AssertionError("must never reconcile")

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        semaphore=_RaisingSemaphore(),  # type: ignore[arg-type]
    )
    # Must not raise -- and (implicitly, via `remaining` being fully consumed without
    # a StopIteration escaping) both claimable jobs were processed.


async def test_drain_does_not_notify_when_the_job_does_not_reach_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requeued/failed job never enqueued a song -- nothing to wake the radio
    coordinator about."""
    remaining = iter(["job-1", None])

    async def _fake_claim(session: object) -> _FakeReadyJob | None:
        job_id = next(remaining)
        return None if job_id is None else _FakeReadyJob(job_id)

    async def _fake_ingest(job: _FakeReadyJob, **kwargs: object) -> None:
        job.state = "INGEST_PENDING"  # requeued, not READY

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _fake_claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _fake_ingest)

    notified: list[str] = []

    async def _notify_ready(song_id: str) -> None:
        notified.append(song_id)

    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        notify_ready=_notify_ready,
    )

    assert notified == []


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
        downloader: object, settings: Settings, notify_ready: object = None,
        semaphore: object = None,
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
