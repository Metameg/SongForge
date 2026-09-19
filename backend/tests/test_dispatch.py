"""Unit tests for the dispatch decision (issue #12, criteria #3 + #4, 429 handling).

Drives ``songforge.jobs.dispatch.dispatch_claimed_job`` with a claimed (in-memory,
no-session) ``Job`` plus injected fakes for the semaphore and generation client — the
London-school seam this module is built for (see its docstring: claiming is
``worker/dispatch.py``'s DB concern, tested for real in
``tests/test_jobs_queue_integration.py``; this is pure decision logic).

Production change that turns these green: implementing ``dispatch_claimed_job`` in
``songforge/jobs/dispatch.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from songforge.config import Settings
from songforge.jobs.dispatch import dispatch_claimed_job
from songforge.jobs.generation_client import (
    GenerationHandles,
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
)
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)


class _RecordingSemaphore:
    """File-local fake ``Semaphore`` that logs calls (with the user id) onto a shared
    ``events`` list so ordering against the generation client is directly observable."""

    def __init__(self, events: list[str], *, acquire_result: bool = True) -> None:
        self._events = events
        self._acquire_result = acquire_result
        self.reconciled_with: int | None = None

    async def acquire(self, user_id: str) -> bool:
        self._events.append(f"semaphore.acquire:{user_id}")
        return self._acquire_result

    async def release(self, user_id: str) -> None:
        self._events.append(f"semaphore.release:{user_id}")

    async def reconcile_from_active_count(self, count: int) -> None:
        self._events.append(f"semaphore.reconcile:{count}")
        self.reconciled_with = count


class _StubGenerationClient:
    """File-local fake ``GenerationClient``: returns/raises a fixed outcome, logging
    onto the same shared ``events`` list as the semaphore fake (ordering proof)."""

    def __init__(
        self, events: list[str], *, outcome: GenerationHandles | Exception
    ) -> None:
        self._events = events
        self._outcome = outcome

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        self._events.append("client.create")
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def _never_called_counter() -> int:
    raise AssertionError("count_active_jobs must only be consulted on the 429 path")


def _claimed_job(**overrides: object) -> Job:
    """A job already claimed + transitioned to SUBMITTING (this module's precondition)."""
    defaults: dict[str, object] = dict(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="a song about testing",
        lyrics=None,
        state=JOB_STATE_SUBMITTING,
        attempts=0,
        available_at=datetime.now(timezone.utc),
        webhook_url="http://web:8000/api/generation/webhook",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


async def test_happy_path_acquires_before_calling_the_client_and_stores_handles() -> None:
    events: list[str] = []
    handles = GenerationHandles(
        task_id="t1", conversion_id_1="c1", conversion_id_2="c2", eta=120, credit_estimate=1.5
    )
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=handles)
    job = _claimed_job()
    settings = Settings()

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert handled is True
    # Ordering IS the criterion: semaphore acquired strictly before the API call.
    assert events == ["semaphore.acquire:user-1", "client.create"]
    assert job.state == JOB_STATE_WAITING_FOR_WEBHOOK
    assert job.task_id == "t1"
    assert job.conversion_id_1 == "c1"
    assert job.conversion_id_2 == "c2"
    assert job.eta == 120
    assert job.credit_estimate == 1.5
    # The slot stays held through WAITING_FOR_WEBHOOK -- not released on the happy path.
    assert not any(e.startswith("semaphore.release") for e in events)


async def test_no_slot_available_never_calls_the_client() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events, acquire_result=False)
    client = _StubGenerationClient(events, outcome=AssertionError("must not be called"))
    job = _claimed_job()
    settings = Settings()

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert handled is False
    assert events == ["semaphore.acquire:user-1"]
    assert "client.create" not in events


async def test_rate_limited_releases_reconciles_and_requeues_with_backoff() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationRateLimited("slow down"))
    job = _claimed_job(attempts=0)
    settings = Settings(dispatch_requeue_backoff_seconds=30)

    before = datetime.now(timezone.utc)
    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=lambda: _fixed_count(4),
    )

    assert handled is True
    assert job.state == JOB_STATE_QUEUED  # requeued, NOT failed
    assert job.attempts == 1
    assert job.available_at >= before + timedelta(seconds=30)
    assert semaphore.reconciled_with == 4
    assert events == [
        "semaphore.acquire:user-1",
        "client.create",
        "semaphore.release:user-1",
        "semaphore.reconcile:4",
    ]
    assert job.task_id is None  # no handle -- nothing was issued to store


async def test_terminal_4xx_marks_failed_and_releases_the_slot_exactly_once() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationRejected(400, "bad prompt"))
    job = _claimed_job()
    settings = Settings()

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert handled is True
    assert job.state == JOB_STATE_FAILED
    assert events.count("semaphore.release:user-1") == 1
    assert not any(e.startswith("semaphore.reconcile") for e in events)


async def test_transient_error_releases_and_requeues_with_backoff_not_failed() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationTransientError("timeout"))
    job = _claimed_job(attempts=2)
    settings = Settings(dispatch_requeue_backoff_seconds=15)

    before = datetime.now(timezone.utc)
    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert handled is True
    assert job.state == JOB_STATE_QUEUED  # not FAILED -- watchdog owns full retry later
    assert job.attempts == 3
    assert job.available_at >= before + timedelta(seconds=15)
    assert events.count("semaphore.release:user-1") == 1
    assert not any(e.startswith("semaphore.reconcile") for e in events)


async def _fixed_count(n: int) -> int:
    return n


# ── Semaphore-release NOTIFY (orchestrator directive extending criterion #5) ──────
#
# "New-job AND semaphore-release LISTEN/NOTIFY wake dispatch" -- so whenever dispatch
# releases a slot (429 / terminal / transient requeue), it must call the injected
# `notify_release` hook with the job id, so a dispatcher parked on a full cap wakes
# promptly instead of relying on the poll backstop. The happy path never releases, so
# it must never notify either.


async def test_rate_limited_release_emits_semaphore_release_notify() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationRateLimited("slow down"))
    job = _claimed_job()
    settings = Settings()
    notified: list[str] = []

    async def _notify(job_id: str) -> None:
        notified.append(job_id)

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=lambda: _fixed_count(0),
        notify_release=_notify,
    )

    assert handled is True
    assert notified == ["job-1"]


async def test_terminal_4xx_release_emits_semaphore_release_notify() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationRejected(400, "bad prompt"))
    job = _claimed_job()
    settings = Settings()
    notified: list[str] = []

    async def _notify(job_id: str) -> None:
        notified.append(job_id)

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
        notify_release=_notify,
    )

    assert handled is True
    assert notified == ["job-1"]


async def test_transient_error_release_emits_semaphore_release_notify() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationTransientError("timeout"))
    job = _claimed_job()
    settings = Settings()
    notified: list[str] = []

    async def _notify(job_id: str) -> None:
        notified.append(job_id)

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
        notify_release=_notify,
    )

    assert handled is True
    assert notified == ["job-1"]


async def test_happy_path_never_emits_semaphore_release_notify() -> None:
    events: list[str] = []
    handles = GenerationHandles(
        task_id="t1", conversion_id_1="c1", conversion_id_2="c2", eta=60, credit_estimate=1.0
    )
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=handles)
    job = _claimed_job()
    settings = Settings()
    notified: list[str] = []

    async def _notify(job_id: str) -> None:
        notified.append(job_id)

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
        notify_release=_notify,
    )

    assert handled is True
    assert notified == []


async def test_no_slot_never_emits_semaphore_release_notify() -> None:
    events: list[str] = []
    semaphore = _RecordingSemaphore(events, acquire_result=False)
    client = _StubGenerationClient(events, outcome=AssertionError("must not be called"))
    job = _claimed_job()
    settings = Settings()
    notified: list[str] = []

    async def _notify(job_id: str) -> None:
        notified.append(job_id)

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
        notify_release=_notify,
    )

    assert handled is False
    assert notified == []


async def test_notify_release_defaults_to_a_noop_when_not_supplied() -> None:
    """`notify_release` is optional -- omitting it (as every other test in this file
    does) must not raise, even on a release path."""
    events: list[str] = []
    semaphore = _RecordingSemaphore(events)
    client = _StubGenerationClient(events, outcome=GenerationRejected(400, "bad prompt"))
    job = _claimed_job()
    settings = Settings()

    handled = await dispatch_claimed_job(
        job,
        semaphore=semaphore,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert handled is True
    assert job.state == JOB_STATE_FAILED
