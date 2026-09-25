"""Watchdog: leaderless periodic recovery sweep (issue #16, acceptance criteria A1-A5).

Mirrors `songforge.jobs.dispatch` / `songforge.jobs.ingest`'s split exactly: a
`claim_*` function does the `FOR UPDATE SKIP LOCKED` DB claim (`worker/watchdog.py`'s
future I/O wiring calls these), and a `sweep_*` function is the pure-ish decision step
that takes an already-claimed `Job` plus injected ports so the recovery/refund logic
is unit-testable without any live datastore (A1: "every side-effecting action is
row-claimed before acting").

Four independent sweeps, one per stranded/terminal case (see
`.orchestrator/CONTEXT.md` "What's MISSING" #4 for the full acceptance mapping):

- `sweep_waiting_overdue` (A2): an overdue `WAITING_FOR_WEBHOOK` job is polled via
  `/byId` -- COMPLETED recovers a lost webhook (INGEST_PENDING, NOTIFY ingest, NO
  re-charge -- the generation API is never called again); ERROR/FAILED -> FAILED (the
  terminal sweep refunds+notifies later, D2); IN_QUEUE is left alone.
- `sweep_submitting_stuck` (A2/A3): a `SUBMITTING` job with no `task_id` past its
  lease is presumed crashed mid-submit and requeued to QUEUED -- an accepted, bounded,
  DELAYED re-call (D6: the lease age-gate is what keeps a resulting double-charge rare
  and delayed, not a bug to eliminate).
- `sweep_ingest_overdue` (A2 backstop): an `INGEST_PENDING` job stalled well past the
  inline retry's own backoff is nudged for re-claim, or escalated to FAILED once
  `ingest_attempts` is already at the ceiling. A BACKSTOP over (not a replacement for)
  `jobs.ingest`'s own bounded retry (D1) -- never rips it out.
- `sweep_terminal_failures` (A4): a `FAILED` job not yet handled is refunded
  (`RateLimiter.refund`, reconstructing the exact `Identity` + ip that created it,
  D3), notified over its per-user SSE channel, and stamped `failure_handled_at` --
  idempotent by construction (a re-claim only ever finds unhandled rows, D2). NEVER
  auto-regenerates -- "allows resubmit" is simply the user POSTing `/create` again
  with their refunded slot.

The four `claim_*` functions are mechanical "SELECT ... FOR UPDATE SKIP LOCKED"
queries, identical in shape to `jobs.dispatch.claim_next_job` /
`jobs.ingest.claim_next_ingest_job`, and are unit-testable in their own right against
in-memory SQLite. Every Redis-facing side effect a `sweep_*` triggers (the NOTIFY hooks,
the quota refund, the per-user publish) is wrapped in a `_safe_*` helper (log +
swallow) -- a Redis blip must never abort the Postgres commit the caller makes right
after (repo convention #3/#4).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_ERROR,
    GENERATION_STATUS_FAILED,
    GenerationClient,
)
from songforge.logging_setup import get_logger
from songforge.metrics import (
    quota_refunded_total,
    user_notifications_total,
    watchdog_recovered_total,
)
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_QUEUED,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)
from songforge.web.rate_limit import Identity, RateLimiter

log = get_logger(__name__)


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive `datetime` to UTC-aware.

    Defensive: SQLite (unit tests) does not round-trip `tzinfo` through a
    `DateTime(timezone=True)` column the way Postgres (production) always does --
    once a `Job` instance leaves the ORM session's identity map (e.g. no strong
    Python reference survives between the writing and reading query), a freshly
    deserialized row's `updated_at` can come back naive even though it was always
    written UTC-aware. A no-op for every already-aware value (i.e. always in
    production).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

# A generic `notify(channel, payload)` hook -- the same shape as
# `jobs.dispatch.NotifyReleaseFn`/`web.routes.create.Notifier`, reused here so
# `sweep_waiting_overdue` (ingest wake) and `sweep_submitting_stuck` (new-job wake)
# can each be handed the real `pg_notify` wiring or a test spy.
NotifyFn = Callable[[str, str], Awaitable[None]]

# Publishes a per-user notification (issue #16 criterion A4): `publish(user_id,
# message)`, where `message` is the JSON string body a connected `/events` client
# receives as a `job-failed` SSE frame. Production wires this to
# `redis.publish(user_channel(settings.user_events_channel_prefix, user_id), message)`
# (see `radio/user_events.py`).
PublishUserEventFn = Callable[[str, str], Awaitable[None]]


async def _noop_notify(channel: str, payload: str) -> None:
    return None


async def _noop_publish_user_event(user_id: str, message: str) -> None:
    return None


async def _safe_notify(notify: NotifyFn, channel: str, payload: str) -> None:
    """Best-effort wake-up NOTIFY (repo convention: a missed NOTIFY only costs the
    poll backstop's latency, never correctness -- see `jobs.dispatch._safe_notify`).
    Must never abort the caller's already-decided state transition."""
    try:
        await notify(channel, payload)
    except Exception:
        log.exception("watchdog_notify_failed", channel=channel, payload=payload)


async def _safe_refund(rate_limiter: RateLimiter, job: Job) -> None:
    """Best-effort quota refund (Postgres = truth, Redis = derived/best-effort, repo
    convention #3): a Redis blip must never abort the commit that stamps
    `failure_handled_at`."""
    identity = Identity(
        user_id=job.user_id, is_authenticated=job.is_authenticated, minted=False
    )
    try:
        await rate_limiter.refund(identity, job.client_ip or "")
    except Exception:
        log.exception("watchdog_refund_failed", job_id=job.job_id)


async def _safe_publish_user_event(
    publish: PublishUserEventFn, job: Job, message: str
) -> None:
    """Best-effort per-user SSE notify -- same convention as `_safe_notify`/
    `_safe_refund`: a Redis blip must never abort the `failure_handled_at` stamp."""
    try:
        await publish(job.user_id, message)
    except Exception:
        log.exception("watchdog_publish_user_event_failed", job_id=job.job_id)


# ── Decision: sweep_waiting_overdue (A2) ──────────────────────────────────────────


async def sweep_waiting_overdue(
    job: Job,
    *,
    client: GenerationClient,
    settings: Settings,
    notify_ingest: NotifyFn | None = None,
) -> None:
    """Poll `/byId` for an already-claimed, overdue `WAITING_FOR_WEBHOOK` job and
    branch on status (A2): COMPLETED -> record `audio_url`/`title`/`audio_duration`,
    transition to INGEST_PENDING, NOTIFY the ingest channel (recovers a lost webhook
    with NO re-charge -- `client.create` must never be called here). ERROR/FAILED ->
    FAILED (the terminal sweep refunds+notifies later, D2). IN_QUEUE / still
    generating -> left WAITING.

    Mutates `job` in place; the caller (`worker/watchdog.py`) commits.
    """
    notify = notify_ingest or _noop_notify
    if job.task_id is None:
        # Data-integrity guard (mirrors `jobs.ingest`'s guards): dispatch always sets
        # `task_id` before a job reaches WAITING_FOR_WEBHOOK, but this must never
        # crash the watchdog loop if it somehow doesn't.
        log.error("watchdog_waiting_missing_task_id", job_id=job.job_id)
        return

    status = await client.get_status_by_id(job.task_id)

    if status.status == GENERATION_STATUS_COMPLETED:
        job.audio_url = status.audio_url
        job.audio_duration = status.duration
        job.title = status.title
        job.state = JOB_STATE_INGEST_PENDING
        watchdog_recovered_total.labels(path="waiting_polled").inc()
        log.info("watchdog_waiting_recovered", job_id=job.job_id)
        await _safe_notify(notify, settings.ingest_channel, job.job_id)
    elif status.status in (GENERATION_STATUS_ERROR, GENERATION_STATUS_FAILED):
        job.state = JOB_STATE_FAILED
        watchdog_recovered_total.labels(path="waiting_polled").inc()
        log.info(
            "watchdog_waiting_terminal", job_id=job.job_id, status=status.status
        )
    else:
        # IN_QUEUE / still generating -- left WAITING. Deliberately not counted as
        # "recovered" (nothing was recovered) and `updated_at` is not bumped (see
        # `claim_waiting_overdue_job`'s docstring: bumping it would push the next
        # overdue check out a full `eta` window, which is wrong once already overdue).
        log.debug("watchdog_waiting_still_pending", job_id=job.job_id)


# ── Decision: sweep_submitting_stuck (A2/A3) ──────────────────────────────────────


async def sweep_submitting_stuck(
    job: Job,
    *,
    settings: Settings,
    notify_new_job: NotifyFn | None = None,
) -> None:
    """Requeue an already-claimed, lease-expired `SUBMITTING` job (no `task_id` --
    the crashed-worker-mid-submit case) back to QUEUED with `attempts` bumped and a
    new-job NOTIFY (A2/A3). The claim query (`claim_submitting_stuck_job`) already
    applied the lease age-gate (D6) -- this function's job is only the transition.

    Mutates `job` in place; the caller commits.
    """
    notify = notify_new_job or _noop_notify
    job.state = JOB_STATE_QUEUED
    job.attempts += 1
    job.available_at = datetime.now(timezone.utc)
    watchdog_recovered_total.labels(path="submitting_recalled").inc()
    log.info("watchdog_submitting_recalled", job_id=job.job_id, attempts=job.attempts)
    await _safe_notify(notify, settings.jobs_new_channel, job.job_id)


# ── Decision: sweep_ingest_overdue (A2 backstop) ──────────────────────────────────


async def sweep_ingest_overdue(
    job: Job,
    *,
    settings: Settings,
) -> None:
    """Backstop (D1) over an already-claimed, overdue `INGEST_PENDING` job: nudge
    `available_at` to now so the ingest loop re-claims it, or escalate to FAILED once
    `ingest_attempts` is already at `settings.ingest_max_attempts` (A2). NOT a
    replacement for `jobs.ingest`'s own inline bounded retry -- only a backstop for a
    stalled/crashed claim that never got that far.

    Mutates `job` in place; the caller commits.
    """
    if job.ingest_attempts >= settings.ingest_max_attempts:
        job.state = JOB_STATE_FAILED
        watchdog_recovered_total.labels(path="ingest_escalated_failed").inc()
        log.info(
            "watchdog_ingest_escalated_failed",
            job_id=job.job_id,
            ingest_attempts=job.ingest_attempts,
        )
    else:
        job.available_at = datetime.now(timezone.utc)
        watchdog_recovered_total.labels(path="ingest_nudged").inc()
        log.info("watchdog_ingest_nudged", job_id=job.job_id)


# ── Decision: sweep_terminal_failures (A4) ────────────────────────────────────────


async def sweep_terminal_failures(
    job: Job,
    *,
    rate_limiter: RateLimiter,
    publish_user_event: PublishUserEventFn | None = None,
) -> None:
    """Terminal-failure refund + per-user notify (A4, D2): reconstruct the
    `Identity`/ip that created an already-claimed, unhandled `FAILED` job
    (`job.user_id`/`job.is_authenticated`/`job.client_ip`, D3) and call
    `rate_limiter.refund`, publish a `job-failed` notification on the user's channel,
    then stamp `job.failure_handled_at`. Idempotent by construction: the claim query
    (`claim_terminal_failure_job`) only ever hands back rows with
    `failure_handled_at IS NULL`, so this function runs at most once per FAILED job.
    NEVER calls the generation client and NEVER auto-regenerates -- "allows resubmit"
    is simply the user POSTing `/create` again with their refunded slot.

    Mutates `job` in place; the caller commits.
    """
    await _safe_refund(rate_limiter, job)
    quota_refunded_total.inc()

    publish = publish_user_event or _noop_publish_user_event
    message = json.dumps({"event": "job-failed", "job_id": job.job_id})
    await _safe_publish_user_event(publish, job, message)
    user_notifications_total.labels(type="job_failed").inc()

    job.failure_handled_at = datetime.now(timezone.utc)
    log.info("watchdog_terminal_failure_handled", job_id=job.job_id)


# ── Claims: FOR UPDATE SKIP LOCKED row-claim queries (A1) ─────────────────────────
#
# Each mirrors `jobs.dispatch.claim_next_job` / `jobs.ingest.claim_next_ingest_job`'s
# shape exactly: a plain `SELECT ... FOR UPDATE SKIP LOCKED` (a no-op locking clause
# on SQLite, same precedent as those two and `web/routes/webhook.py`'s lookup) so
# every worker instance can run every sweep concurrently without double-acting on the
# same row. Unlike `claim_next_job`, none of these eagerly transition state on claim
# -- the decision (`sweep_*`) differs by outcome, so the claim only locks + selects.


async def claim_waiting_overdue_job(session: AsyncSession, settings: Settings) -> Job | None:
    """Claim the oldest overdue `WAITING_FOR_WEBHOOK` job: design D5's
    `updated_at + eta + buffer <= now`. `eta` (seconds) varies per row, so this isn't
    a single portable SQL predicate across SQLite (unit tests) and Postgres
    (production) -- fetch a bounded, oldest-first batch under `FOR UPDATE SKIP
    LOCKED` (so concurrent sweeps never contend on the same rows even while
    scanning), then apply the precise per-row check in Python and hand back the
    first match.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(Job.state == JOB_STATE_WAITING_FOR_WEBHOOK)
        .order_by(Job.updated_at)
        .limit(settings.watchdog_claim_batch_size)
        .with_for_update(skip_locked=True)
    )
    candidates = (await session.scalars(stmt)).all()
    for job in candidates:
        eta_seconds = job.eta or 0
        overdue_at = _as_aware_utc(job.updated_at) + timedelta(
            seconds=eta_seconds + settings.watchdog_waiting_overdue_buffer_seconds
        )
        if overdue_at <= now:
            return job
    return None


async def claim_submitting_stuck_job(session: AsyncSession, settings: Settings) -> Job | None:
    """Claim the oldest `SUBMITTING` job with no `task_id` whose `updated_at` is
    older than `settings.watchdog_submitting_lease_seconds` -- the crashed-worker-
    mid-submit case (A2/A3), age-gated so the resulting delayed re-call is bounded
    and rare (D6)."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.watchdog_submitting_lease_seconds
    )
    stmt = (
        select(Job)
        .where(
            Job.state == JOB_STATE_SUBMITTING,
            Job.task_id.is_(None),
            Job.updated_at <= cutoff,
        )
        .order_by(Job.updated_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.scalars(stmt)).first()


async def claim_ingest_overdue_job(session: AsyncSession, settings: Settings) -> Job | None:
    """Claim the oldest `INGEST_PENDING` job whose `updated_at` is older than the
    GENEROUS `settings.watchdog_ingest_overdue_seconds` threshold -- a backstop over
    (not a replacement for) `jobs.ingest`'s own inline retry (D1)."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.watchdog_ingest_overdue_seconds
    )
    stmt = (
        select(Job)
        .where(Job.state == JOB_STATE_INGEST_PENDING, Job.updated_at <= cutoff)
        .order_by(Job.updated_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.scalars(stmt)).first()


async def claim_terminal_failure_job(session: AsyncSession) -> Job | None:
    """Claim the oldest `FAILED` job not yet handled (`failure_handled_at IS NULL`,
    D2) -- the row-claim guard that makes `sweep_terminal_failures`'s refund+notify
    idempotent (a re-claim of an already-stamped row is never returned here)."""
    stmt = (
        select(Job)
        .where(Job.state == JOB_STATE_FAILED, Job.failure_handled_at.is_(None))
        .order_by(Job.seq)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.scalars(stmt)).first()
