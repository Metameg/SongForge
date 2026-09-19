"""Dispatch decision: a claimed job -> semaphore -> generation API -> state transition.

Issue #12 criteria #3 (the semaphore is acquired BEFORE the API call and the global cap
is never exceeded) and #4 (SUBMITTING -> WAITING_FOR_WEBHOOK, storing the returned
handles). Pure-ish: takes an already-claimed ``Job`` (state already ``SUBMITTING`` —
claiming via ``FOR UPDATE SKIP LOCKED`` is ``worker/dispatch.py``'s DB-claim wiring,
exercised against real Postgres in ``tests/test_jobs_queue_integration.py``) plus
injected ports (semaphore, generation client, an active-job counter used only on the
429 path) so the decision/transition logic is unit-testable without any live datastore.

429 handling (``.orchestrator/CONTEXT.md`` "In-scope decision"): the generation API's
429 is the *authoritative* backstop over the best-effort Redis semaphore (PRD #18). On
429: release the optimistic slot, reconcile the semaphore from Postgres truth (a fresh
count of active-state rows), requeue the job to QUEUED with backoff. Never FAILED,
never refunded — no charge was made, no handle was issued.

On a terminal 4xx (bad prompt / insufficient credits): FAILED, slot released once. On a
5xx or network timeout: release the slot and requeue with backoff too, so dispatch never
wedges holding a slot on a job it can't currently place — full retry-budget/backoff-curve
ownership is the (out-of-scope, later) watchdog's job; this only prevents a stuck slot.

``notify_release`` (an orchestrator directive extending criterion #5: "New-job AND
semaphore-release LISTEN/NOTIFY wake dispatch") is called with the released job's id on
every release path (429/terminal/transient) so a dispatcher parked on a full cap wakes
promptly instead of waiting for the poll backstop. It is optional (defaults to a no-op)
so existing callers/tests that don't care about the wake-up don't need to supply one;
``worker/dispatch.py`` wires the real ``pg_notify`` implementation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GenerationClient,
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
)
from songforge.jobs.semaphore import Semaphore
from songforge.logging_setup import get_logger
from songforge.metrics import jobs_dispatched_total, jobs_failed_total, jobs_requeued_total
from songforge.models import (
    ACTIVE_JOB_STATES,
    JOB_STATE_FAILED,
    JOB_STATE_QUEUED,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)

log = get_logger(__name__)

# Supplies a fresh count of active-state job rows for the 429 reconcile step. Injected
# so the pure decision function stays unit-testable without a live Postgres session.
ActiveCountProvider = Callable[[], Awaitable[int]]

# Called with a released job's id on every semaphore-release path. Optional (defaults
# to a no-op) -- production wires it to a real `pg_notify(semaphore_release_channel,
# job_id)` (see `worker/dispatch.py`).
NotifyReleaseFn = Callable[[str], Awaitable[None]]


async def _noop_notify_release(job_id: str) -> None:
    return None


async def _safe_release(semaphore: Semaphore, user_id: str) -> None:
    """Best-effort slot release: this repo's convention is that a Redis blip must
    never abort an otherwise-committed Postgres state transition (see
    ``radio/coordinator.py::_write_pointer_best_effort``). Logged and swallowed --
    worst case the counter is corrected later by the 429 path's
    ``reconcile_from_active_count`` (Postgres is the source of truth) or the
    (out-of-scope, later) watchdog."""
    try:
        await semaphore.release(user_id)
    except Exception:
        log.exception("semaphore_release_failed", user_id=user_id)


async def _safe_reconcile(
    semaphore: Semaphore, count_active_jobs: ActiveCountProvider
) -> None:
    """Best-effort reconcile (429 path only) -- same convention as ``_safe_release``."""
    try:
        active_count = await count_active_jobs()
        await semaphore.reconcile_from_active_count(active_count)
    except Exception:
        log.exception("semaphore_reconcile_failed")


async def _safe_notify(notify: NotifyReleaseFn, job_id: str) -> None:
    """Best-effort wake-up notify: a missed NOTIFY only costs the poll backstop's
    latency, never correctness, so it must never abort a state transition either."""
    try:
        await notify(job_id)
    except Exception:
        log.exception("notify_release_failed", job_id=job_id)


async def dispatch_claimed_job(
    job: Job,
    *,
    semaphore: Semaphore,
    client: GenerationClient,
    settings: Settings,
    count_active_jobs: ActiveCountProvider,
    notify_release: NotifyReleaseFn | None = None,
) -> bool:
    """Drive one claimed (``state == SUBMITTING``) job through semaphore + API call.

    Mutates ``job`` in place (state, handles, attempts/available_at); the caller
    (``worker/dispatch.py``) owns the DB commit. Returns ``True`` if a slot was
    acquired and the API was called (regardless of outcome), ``False`` if no slot was
    available — the job is left for the caller to leave QUEUED without ever having
    called the API (criterion #3: "no slot -> don't claim/hold").
    """
    notify = notify_release or _noop_notify_release

    acquired = await semaphore.acquire(job.user_id)
    if not acquired:
        job.state = JOB_STATE_QUEUED
        log.info("dispatch_no_slot", job_id=job.job_id, user_id=job.user_id)
        # Accepted v1 trade-off (PRD v1-FIFO, spec #61-63): strict seq-order FIFO
        # claiming (criterion #2) means a per-user-capped job at the head of the
        # queue can bounce here and stop this drain pass (see `_drain_ready_jobs`
        # in `worker/dispatch.py`) before trying the next, non-saturated user's job
        # -- even with spare global capacity. Deliberate, not a bug: a fairness/
        # skip-ahead pass is a later issue's concern (quality report MED finding).
        return False

    try:
        handles = await client.create(
            prompt=job.prompt,
            lyrics=job.lyrics,
            webhook_url=job.webhook_url or settings.musicgpt_webhook_url,
        )
    except GenerationRateLimited:
        _apply_backoff(job, settings)
        await _safe_release(semaphore, job.user_id)
        await _safe_reconcile(semaphore, count_active_jobs)
        await _safe_notify(notify, job.job_id)
        jobs_requeued_total.labels(reason="rate_limited").inc()
        log.info(
            "dispatch_rate_limited_requeued", job_id=job.job_id, attempts=job.attempts
        )
        return True
    except GenerationRejected as exc:
        job.state = JOB_STATE_FAILED
        await _safe_release(semaphore, job.user_id)
        await _safe_notify(notify, job.job_id)
        jobs_failed_total.inc()
        log.info(
            "dispatch_failed_terminal", job_id=job.job_id, status_code=exc.status_code
        )
        return True
    except GenerationTransientError:
        _apply_backoff(job, settings)
        await _safe_release(semaphore, job.user_id)
        await _safe_notify(notify, job.job_id)
        jobs_requeued_total.labels(reason="transient").inc()
        log.info("dispatch_transient_requeued", job_id=job.job_id, attempts=job.attempts)
        return True
    except Exception:
        # Catch-all (quality report HIGH finding): anything unmapped here -- a bug,
        # or some other surprise `client.create()` didn't turn into one of the three
        # typed exceptions above -- must be handled at least as safely as the
        # 5xx/timeout branch: release the slot, requeue with backoff, notify. The
        # job must never be left stranded in SUBMITTING, and the slot must never
        # leak (a malformed-200 body specifically is now handled upstream in
        # `generation_client.HttpGenerationClient.create`, which raises
        # `GenerationTransientError` for that case -- this branch is the backstop
        # for anything else).
        log.exception("dispatch_unmapped_error", job_id=job.job_id)
        _apply_backoff(job, settings)
        await _safe_release(semaphore, job.user_id)
        await _safe_notify(notify, job.job_id)
        jobs_requeued_total.labels(reason="unmapped_error").inc()
        return True

    job.state = JOB_STATE_WAITING_FOR_WEBHOOK
    job.task_id = handles.task_id
    job.conversion_id_1 = handles.conversion_id_1
    job.conversion_id_2 = handles.conversion_id_2
    job.eta = handles.eta
    job.credit_estimate = handles.credit_estimate
    jobs_dispatched_total.inc()
    log.info("dispatch_submitted", job_id=job.job_id, task_id=handles.task_id)
    return True


def _apply_backoff(job: Job, settings: Settings) -> None:
    """Requeue ``job`` to QUEUED with backoff (shared by the 429 and 5xx/timeout paths)."""
    job.state = JOB_STATE_QUEUED
    job.attempts += 1
    job.available_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.dispatch_requeue_backoff_seconds
    )


async def claim_next_job(session: AsyncSession) -> Job | None:
    """Claim the oldest available QUEUED job and transition it to SUBMITTING.

    ``FOR UPDATE SKIP LOCKED`` (criterion #2) lets every worker instance run this
    concurrently without double-claiming; ordering by ``seq`` walks the partial index
    ``ix_jobs_queued_seq`` (migration ``0003_jobs``) FIFO. Commits the transition
    itself so the row is visible (and its lock released) to other workers immediately,
    rather than holding the row locked for the duration of the API call that follows.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(Job.state == JOB_STATE_QUEUED, Job.available_at <= now)
        .order_by(Job.seq)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.scalars(stmt)).first()
    if job is None:
        return None
    job.state = JOB_STATE_SUBMITTING
    await session.commit()
    return job


async def count_active_jobs(session: AsyncSession) -> int:
    """Fresh count of active-state job rows (the 429 reconcile path's Postgres truth)."""
    result = await session.scalar(
        select(func.count()).select_from(Job).where(Job.state.in_(ACTIVE_JOB_STATES))
    )
    return int(result or 0)
