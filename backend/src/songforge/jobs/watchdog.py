"""Watchdog: leaderless periodic recovery sweep (issue #16, acceptance criteria A1-A5).

Mirrors `songforge.jobs.dispatch` / `songforge.jobs.ingest`'s split exactly: a
`claim_*` function does the `FOR UPDATE SKIP LOCKED` DB claim (`worker/watchdog.py`'s
I/O wiring calls these), and a `sweep_*` function is the pure-ish decision step that
takes an already-claimed `Job` and mutates it in place so the recovery/refund logic
is unit-testable without any live datastore (A1: "every side-effecting action is
row-claimed before acting").

Every `sweep_*` is a PURE state-transition step: it mutates `job` and returns any
Redis-facing side effect the caller should fire, as data (a `NotifyIntent` tuple, or
`TerminalFailureIntent`), rather than calling out to Redis itself. `worker/watchdog.py`
commits the Postgres transition FIRST, and only then fires the returned intent
best-effort (mirrors `worker/ingest.py::_drain_ingest_pending`'s `ready_song_id`
hand-back-then-notify-outside-the-session shape). This closes two gaps a notify/
refund-from-inside-the-sweep shape would otherwise leave open: a NOTIFY fired before
the commit lands could wake a consumer that finds the old, uncommitted state; and a
refund fired before `failure_handled_at` is committed could double-refund if the
process crashes between the two (a crash AFTER commit, before the post-commit refund,
now costs at most one MISSED refund -- never a double one).

Four independent sweeps, one per stranded/terminal case (see
`.orchestrator/CONTEXT.md` "What's MISSING" #4 for the full acceptance mapping):

- `sweep_waiting_overdue` (A2): an overdue `WAITING_FOR_WEBHOOK` job is polled via
  `/byId` -- COMPLETED recovers a lost webhook (INGEST_PENDING, returns a
  `NotifyIntent` for the ingest channel, NO re-charge -- the generation API is never
  called again); ERROR/FAILED -> FAILED (the terminal sweep refunds+notifies later,
  D2); IN_QUEUE is left alone. A `/byId` call that itself fails (429/5xx/timeout/4xx)
  leaves the job WAITING untouched -- see the module-level exception-safety note below.
- `sweep_submitting_stuck` (A2/A3): a `SUBMITTING` job with no `task_id` past its
  lease is presumed crashed mid-submit and requeued to QUEUED -- an accepted, bounded,
  DELAYED re-call (D6: the lease age-gate is what keeps a resulting double-charge rare
  and delayed, not a bug to eliminate).
- `sweep_ingest_overdue` (A2 backstop): an `INGEST_PENDING` job stalled well past the
  inline retry's own backoff is nudged for re-claim, or escalated to FAILED once
  `ingest_attempts` is already at the ceiling. A BACKSTOP over (not a replacement for)
  `jobs.ingest`'s own bounded retry (D1) -- never rips it out.
- `sweep_terminal_failures` (A4): a `FAILED` job not yet handled has `failure_handled_at`
  stamped and returns a `TerminalFailureIntent` (the reconstructed `Identity` + ip +
  charge-day + user/job ids, D3) for the caller to refund (`RateLimiter.refund`) and
  notify over the user's per-user SSE channel AFTER the stamp commits -- idempotent by
  construction (a re-claim only ever finds unhandled rows, D2). NEVER auto-regenerates
  -- "allows resubmit" is simply the user POSTing `/create` again with their refunded
  slot.

The four `claim_*` functions are mechanical "SELECT ... FOR UPDATE SKIP LOCKED"
queries, identical in shape to `jobs.dispatch.claim_next_job` /
`jobs.ingest.claim_next_ingest_job`, and are unit-testable in their own right against
in-memory SQLite.

Exception safety (`sweep_waiting_overdue`): a `/byId` poll can itself fail the same
way a `create` call can (429/5xx/timeout/terminal 4xx, `jobs.generation_client`'s
typed exceptions) -- an outage of the generation API's `/byId` endpoint must NEVER
propagate out of this sweep. Unlike `jobs.dispatch.dispatch_claimed_job` (which reacts
differently per exception type -- backoff vs. FAILED), every one of the three typed
exceptions here gets the SAME reaction: log and leave the job WAITING untouched (no
state change, no re-charge, no intent) -- there is nothing else safe to do with an
inconclusive status lookup, and the next watchdog tick tries again. An uncaught
exception here would otherwise unwind through `_run_sweeps`' drain loop and abort the
WHOLE tick, starving the other three sweeps (including the terminal-failure refund)
for a full `watchdog_poll_interval_seconds` -- precisely during the kind of outage the
watchdog exists to survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_ERROR,
    GENERATION_STATUS_FAILED,
    GenerationClient,
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
)
from songforge.logging_setup import get_logger
from songforge.metrics import watchdog_recovered_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_QUEUED,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)
from songforge.web.rate_limit import Identity

log = get_logger(__name__)

# `(channel, payload)` -- a NOTIFY the caller should fire, best-effort, AFTER its
# commit succeeds (see the module docstring). `None` means no NOTIFY is warranted for
# this claimed row (e.g. left WAITING, or a `/byId` failure).
NotifyIntent = tuple[str, str]


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


# ── Decision: sweep_waiting_overdue (A2) ──────────────────────────────────────────


async def sweep_waiting_overdue(
    job: Job,
    *,
    client: GenerationClient,
    settings: Settings,
) -> NotifyIntent | None:
    """Poll `/byId` for an already-claimed, overdue `WAITING_FOR_WEBHOOK` job and
    branch on status (A2): COMPLETED -> record `audio_url`/`title`/`audio_duration`,
    transition to INGEST_PENDING, and return a `NotifyIntent` for the ingest channel
    (recovers a lost webhook with NO re-charge -- `client.create` must never be called
    here). ERROR/FAILED -> FAILED (the terminal sweep refunds+notifies later, D2).
    IN_QUEUE / still generating -> left WAITING. A `/byId` call that itself raises one
    of the typed generation-client exceptions (429/5xx/timeout/terminal 4xx) is caught
    here and treated the same as "still pending": left WAITING, no state change, no
    intent -- see the module docstring's exception-safety note.

    Mutates `job` in place; the caller (`worker/watchdog.py`) commits, then fires the
    returned intent (if any) AFTER that commit succeeds.
    """
    if job.task_id is None:
        # Data-integrity guard (mirrors `jobs.ingest`'s guards): dispatch always sets
        # `task_id` before a job reaches WAITING_FOR_WEBHOOK, but this must never
        # crash the watchdog loop if it somehow doesn't.
        log.error("watchdog_waiting_missing_task_id", job_id=job.job_id)
        return None

    try:
        status = await client.get_status_by_id(job.task_id)
    except (GenerationRateLimited, GenerationTransientError, GenerationRejected) as exc:
        # The generation API's `/byId` endpoint is itself unavailable/erroring. This
        # must NEVER propagate: an uncaught raise here would abort the whole watchdog
        # tick (see module docstring), starving the other three sweeps -- including
        # the terminal-failure refund -- for a full poll interval, precisely during
        # the kind of outage the watchdog exists to survive. Leave WAITING; the next
        # tick tries again.
        log.warning(
            "watchdog_waiting_poll_failed",
            job_id=job.job_id,
            task_id=job.task_id,
            error_type=type(exc).__name__,
        )
        return None

    if status.status == GENERATION_STATUS_COMPLETED:
        job.audio_url = status.audio_url
        job.audio_duration = status.duration
        job.title = status.title
        job.state = JOB_STATE_INGEST_PENDING
        watchdog_recovered_total.labels(path="waiting_polled").inc()
        log.info("watchdog_waiting_recovered", job_id=job.job_id)
        return (settings.ingest_channel, job.job_id)
    elif status.status in (GENERATION_STATUS_ERROR, GENERATION_STATUS_FAILED):
        job.state = JOB_STATE_FAILED
        watchdog_recovered_total.labels(path="waiting_polled").inc()
        log.info(
            "watchdog_waiting_terminal", job_id=job.job_id, status=status.status
        )
        return None
    else:
        # IN_QUEUE / still generating -- left WAITING. Deliberately not counted as
        # "recovered" (nothing was recovered) and `updated_at` is not bumped (see
        # `claim_waiting_overdue_job`'s docstring: bumping it would push the next
        # overdue check out a full `eta` window, which is wrong once already overdue).
        log.debug("watchdog_waiting_still_pending", job_id=job.job_id)
        return None


# ── Decision: sweep_submitting_stuck (A2/A3) ──────────────────────────────────────


async def sweep_submitting_stuck(
    job: Job,
    *,
    settings: Settings,
) -> NotifyIntent:
    """Requeue an already-claimed, lease-expired `SUBMITTING` job (no `task_id` --
    the crashed-worker-mid-submit case) back to QUEUED with `attempts` bumped, and
    return a `NotifyIntent` for the new-job channel (A2/A3). The claim query
    (`claim_submitting_stuck_job`) already applied the lease age-gate (D6) -- this
    function's job is only the transition.

    Mutates `job` in place; the caller commits, then fires the returned intent AFTER
    that commit succeeds. Unlike `sweep_waiting_overdue`, a claimed row here is
    unconditionally requeued, so a `NotifyIntent` is always returned (never `None`).
    """
    job.state = JOB_STATE_QUEUED
    job.attempts += 1
    job.available_at = datetime.now(timezone.utc)
    watchdog_recovered_total.labels(path="submitting_recalled").inc()
    log.info("watchdog_submitting_recalled", job_id=job.job_id, attempts=job.attempts)
    return (settings.jobs_new_channel, job.job_id)


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


@dataclass(frozen=True)
class TerminalFailureIntent:
    """Data the caller needs to refund + notify AFTER `sweep_terminal_failures`'s
    `failure_handled_at` stamp commits (A4, D2, F4): reconstructed here (inside the
    pure decision step, from `job` fields already available) but fired by the caller
    ONLY once that commit has landed -- so a crash between the two costs at most one
    MISSED refund (the user loses a slot; not exploitable), never a double-refund
    (the "commit first, then best-effort side effect" convention, mirroring
    `worker/ingest.py`'s post-commit notify)."""

    identity: Identity
    ip: str
    day: str
    user_id: str
    job_id: str


async def sweep_terminal_failures(job: Job) -> TerminalFailureIntent | None:
    """Terminal-failure handling (A4, D2): stamp `job.failure_handled_at` and return a
    `TerminalFailureIntent` bundling the reconstructed `Identity`/ip/charge-day
    (`job.user_id`/`job.is_authenticated`/`job.client_ip`, D3) the caller needs to
    refund (`RateLimiter.refund`) and notify (the per-user `job-failed` SSE channel)
    AFTER this stamp commits. Idempotent by construction: the claim query
    (`claim_terminal_failure_job`) only ever hands back rows with
    `failure_handled_at IS NULL`; `None` is returned defensively if handed an
    already-stamped row (should never happen given that claim guard, but this function
    must never double-stamp or return a stale intent if it does). NEVER calls the
    generation client and NEVER auto-regenerates -- "allows resubmit" is simply the
    user POSTing `/create` again with their refunded slot.

    The refund's charge-day (F3) is `job.created_at`'s UTC date, NOT "today" -- a job
    created before a UTC-midnight rollover and handled after it must refund the day it
    was actually CHARGED (`consume`'s bucket), never the day the failure happens to be
    swept on (which it never charged) -- see `RateLimiter.refund`'s `day` docstring for
    the farmable-gap this closes.

    Mutates `job` in place; the caller commits, then fires the returned intent.
    """
    if job.failure_handled_at is not None:
        return None

    if not job.client_ip:
        # F5: a pre-migration-0006 row (or any row created before `client_ip` was
        # persisted) refunds only the cookie leg via a decoy `ip=""` key -- silently
        # incomplete otherwise. Surfacing it here keeps that gap observable rather
        # than absorbed into an unremarkable "refunded" log line.
        log.warning("watchdog_refund_missing_client_ip", job_id=job.job_id)

    identity = Identity(
        user_id=job.user_id, is_authenticated=job.is_authenticated, minted=False
    )
    day = _as_aware_utc(job.created_at).date().isoformat()

    job.failure_handled_at = datetime.now(timezone.utc)
    log.info("watchdog_terminal_failure_handled", job_id=job.job_id)

    return TerminalFailureIntent(
        identity=identity,
        ip=job.client_ip or "",
        day=day,
        user_id=job.user_id,
        job_id=job.job_id,
    )


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
