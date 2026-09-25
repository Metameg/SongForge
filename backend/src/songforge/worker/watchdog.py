"""Worker-side watchdog loop: periodic leaderless recovery sweep (issue #16, A1-A5).

Mirrors `worker/dispatch.py` / `worker/ingest.py`'s split: a testable drain step over
`jobs.watchdog`'s claim+sweep functions, and thin I/O wiring (`run_watchdog`) that
loops on a plain interval (recovery is not latency-critical, per
`.orchestrator/CONTEXT.md` -- a simple poll loop is acceptable; no LISTEN/NOTIFY wake
is required). Like `run_dispatch`/`run_ingest`, `run_watchdog` itself has no dedicated
unit test: any error inside it is logged and backed off, never raised, so a broken
watchdog loop never takes down the worker's other supervised loops.

`_run_sweeps` drains EACH of the four sweeps to exhaustion, sequentially (waiting ->
submitting -> ingest -> terminal), one short-lived claiming session per CLAIMED ROW --
mirrors `worker/dispatch.py::_drain_ready_jobs` / `worker/ingest.py::
_drain_ingest_pending`'s "claim, act, commit, repeat until the claim comes back empty"
shape exactly, applied to each of the four sweep types in turn (F2: a single row per
type per tick would let a mass-failure backlog outrun recovery -- draining each type
fully before moving to the next is what makes one tick's worth of recovery actually
catch up).

Every Redis-facing side effect a sweep's outcome calls for (the ingest/new-job NOTIFY,
the quota refund, the per-user publish) is fired ONLY AFTER that row's commit has
succeeded, from the intent the pure `jobs.watchdog.sweep_*` function returned (F4) --
mirrors `worker/ingest.py::_drain_ingest_pending`'s `ready_song_id` hand-back-then-
notify-outside-the-session shape, and closes the crash-between-commit-and-refund
double-refund gap (a crash there now costs at most one MISSED refund, never a double
one). The two outbound NOTIFYs sweep 1 (ingest wake) and sweep 2 (new-job wake) need
are wired to a direct `asyncpg` connection (mirrors `worker/dispatch.py::run_dispatch`'s
own notify connection); sweep 4's per-user notification is wired to a plain
`redis.publish` against the same channel shape `radio.user_events.user_channel`/
`UserEventBroadcaster` use.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from songforge.config import Settings
from songforge.db import get_sessionmaker
from songforge.jobs.dispatch import count_active_jobs, count_active_jobs_by_user
from songforge.jobs.generation_client import GenerationClient, HttpGenerationClient
from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, Semaphore
from songforge.jobs.watchdog import (
    NotifyIntent,
    claim_ingest_overdue_job,
    claim_submitting_stuck_job,
    claim_terminal_failure_job,
    claim_waiting_overdue_job,
    sweep_ingest_overdue,
    sweep_submitting_stuck,
    sweep_terminal_failures,
    sweep_waiting_overdue,
)
from songforge.logging_setup import get_logger
from songforge.metrics import (
    quota_refunded_total,
    semaphore_reconciled_total,
    semaphore_released_total,
    user_notifications_total,
)
from songforge.models import JOB_STATE_FAILED
from songforge.radio.user_events import user_channel
from songforge.redis_client import get_redis
from songforge.web.rate_limit import Identity, RateLimiter, RedisRateLimitBackend

log = get_logger(__name__)

# A generic `notify(channel, payload)` hook -- the same shape as
# `jobs.dispatch.NotifyReleaseFn`/`web.routes.create.Notifier`, reused here to fire a
# `NotifyIntent` (`jobs.watchdog.sweep_waiting_overdue`/`sweep_submitting_stuck`'s
# post-commit return value) via the real `pg_notify` wiring or a test spy.
NotifyFn = Callable[[str, str], Awaitable[None]]

# Publishes a per-user notification (issue #16 criterion A4): `publish(user_id,
# message)`, where `message` is the JSON string body a connected `/events` client
# receives as a `job-failed` SSE frame. Production wires this to
# `redis.publish(user_channel(settings.user_events_channel_prefix, user_id), message)`
# (see `radio/user_events.py`).
PublishUserEventFn = Callable[[str, str], Awaitable[None]]


async def _safe_notify(notify: NotifyFn, channel: str, payload: str) -> None:
    """Best-effort wake-up NOTIFY, fired only after the row's commit has already
    succeeded (repo convention: a missed NOTIFY only costs the poll backstop's
    latency, never correctness -- see `jobs.dispatch._safe_notify`)."""
    try:
        await notify(channel, payload)
    except Exception:
        log.exception("watchdog_notify_failed", channel=channel, payload=payload)


async def _safe_refund(
    rate_limiter: RateLimiter, identity: Identity, ip: str, day: str
) -> None:
    """Best-effort quota refund (Postgres = truth, Redis = derived/best-effort, repo
    convention #3), fired only after `failure_handled_at` has already committed -- a
    Redis blip here costs at most one missed refund, never blocks/undoes the stamp."""
    try:
        await rate_limiter.refund(identity, ip, day=day)
    except Exception:
        log.exception("watchdog_refund_failed", user_id=identity.user_id)


async def _safe_publish_user_event(
    publish: PublishUserEventFn, user_id: str, message: str
) -> None:
    """Best-effort per-user SSE notify -- same convention as `_safe_notify`/
    `_safe_refund`: fired only after `failure_handled_at` has already committed."""
    try:
        await publish(user_id, message)
    except Exception:
        log.exception("watchdog_publish_user_event_failed", user_id=user_id)


async def _safe_release_semaphore(semaphore: Semaphore, user_id: str) -> None:
    """Best-effort generation-semaphore release (issue #16 slot-leak fix, mirrors
    `jobs.dispatch._safe_release`): fired only after a sweep's own commit has already
    succeeded -- a Redis blip here must never surface, and worst case is corrected
    later by `_safe_reconcile_semaphore`'s per-tick backstop below."""
    try:
        await semaphore.release(user_id)
        semaphore_released_total.labels(site="watchdog").inc()
    except Exception:
        log.exception("semaphore_release_failed", site="watchdog", user_id=user_id)


async def _safe_reconcile_semaphore(
    sessionmaker: async_sessionmaker[AsyncSession], semaphore: Semaphore
) -> None:
    """Reconcile backstop (Part B, issue #16): once per tick, snap the global counter
    and every per-user counter Redis currently holds a key for back to a fresh
    Postgres count of `ACTIVE_JOB_STATES` rows -- Postgres is the source of truth, and
    this recovers any slot a release site above never got to fire for (e.g. the
    process was killed between a sweep's commit and its post-commit release).

    Race note: this is read-count-then-set, so a slot acquired between the count and
    the `set_value` is momentarily invisible to this snap. Safe (not a double-spend):
    the generation API's own 429 backstop (PRD #18) is the AUTHORITATIVE cap over the
    best-effort Redis semaphore -- a drifted/reconciled counter can only bounce a few
    extra requests as 429s, never let the true in-flight count exceed the real cap.

    Wrapped as a whole in log+swallow: a Redis outage during reconcile (the global
    set, the per-user SCAN, or any one per-user set) must never crash the tick --
    partial progress from an already-applied set is left as-is rather than rolled
    back, exactly like a partial drain would be.
    """
    try:
        async with sessionmaker() as session:
            active_count = await count_active_jobs(session)
            active_by_user = await count_active_jobs_by_user(session)

        await semaphore.reconcile_from_active_count(active_count)
        semaphore_reconciled_total.labels(scope="global").inc()

        user_ids = await semaphore.scan_user_ids()
        for user_id in user_ids:
            await semaphore.reconcile_user_from_active_count(
                user_id, active_by_user.get(user_id, 0)
            )
            semaphore_reconciled_total.labels(scope="user").inc()
    except Exception:
        log.exception("watchdog_semaphore_reconcile_failed")


async def _drain_waiting_overdue(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: GenerationClient,
    settings: Settings,
    notify: NotifyFn,
    semaphore: Semaphore | None = None,
) -> None:
    """Claim + poll + commit every currently-overdue `WAITING_FOR_WEBHOOK` row, one at
    a time, until none remain (F2) -- mirrors `_drain_ready_jobs`'s "one short-lived
    session per claimed row" shape. A recovered row's ingest-wake NOTIFY fires only
    after that row's own commit succeeds (F4).

    `semaphore` is optional/backward-compatible (issue #16 slot-leak fix): when a row
    becomes FAILED (the `/byId` ERROR/FAILED branch), it has left `ACTIVE_JOB_STATES`
    without ever reaching ingest, so its slot is released, post-commit, best-effort.
    A row recovered to INGEST_PENDING (still active) or left WAITING must NOT release.

    Non-termination-bug fix (phase 5 re-review): a claimed row `sweep_waiting_overdue`
    leaves WAITING untouched -- still IN_QUEUE, or an F1 `/byId` exception -- makes NO
    state change and deliberately does not bump `updated_at`, so without tracking it,
    `claim_waiting_overdue_job` would re-select that SAME row on every following
    iteration of this `while True` loop: an infinite loop within one tick, hammering
    `/byId` and starving `_drain_submitting_stuck`/`_drain_ingest_overdue`/
    `_drain_terminal_failures`, which only run after this one returns. `examined`
    accumulates every job_id claimed this call and is passed as `exclude_job_ids` on
    the next claim, so a still-pending or erroring row is polled AT MOST ONCE per
    tick (the pre-drain-to-exhaustion cadence) while a row that actually transitions
    (COMPLETED/ERROR/FAILED) keeps draining normally in the same loop -- it never
    needs the exclusion, since its state change alone removes it from the claim
    query. A fresh, empty `examined` set starts on every call (i.e. every tick).
    """
    examined: set[str] = set()
    while True:
        async with sessionmaker() as session:
            job = await claim_waiting_overdue_job(
                session, settings, exclude_job_ids=examined
            )
            if job is None:
                return
            examined.add(job.job_id)
            intent = await sweep_waiting_overdue(job, client=client, settings=settings)
            await session.commit()
            # Only touch `job.state`/`job.user_id` when a `semaphore` was actually
            # supplied -- mirrors `worker/ingest.py::_drain_ingest_pending`'s
            # `ready_song_id`/`release_user_id` capture-inside-the-session-block
            # pattern (never touch `job.*` after the session has closed).
            release_user_id = (
                job.user_id
                if semaphore is not None and job.state == JOB_STATE_FAILED
                else None
            )

        if intent is not None:
            channel, payload = intent
            await _safe_notify(notify, channel, payload)
        if semaphore is not None and release_user_id is not None:
            await _safe_release_semaphore(semaphore, release_user_id)


async def _drain_submitting_stuck(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    notify: NotifyFn,
    semaphore: Semaphore | None = None,
) -> None:
    """Claim + requeue + commit every currently lease-expired `SUBMITTING` row, one at
    a time, until none remain (F2). The new-job-wake NOTIFY fires only after each row's
    own commit succeeds (F4).

    `semaphore` is optional/backward-compatible (issue #16 slot-leak fix): a claimed
    row here is UNCONDITIONALLY requeued to QUEUED (out of `ACTIVE_JOB_STATES`), so
    the slot the crashed worker's dispatch acquired is always released -- unlike the
    other two drains, there is no "left active" branch to guard against.
    """
    while True:
        async with sessionmaker() as session:
            job = await claim_submitting_stuck_job(session, settings)
            if job is None:
                return
            intent: NotifyIntent = await sweep_submitting_stuck(job, settings=settings)
            await session.commit()
            release_user_id = job.user_id if semaphore is not None else None

        channel, payload = intent
        await _safe_notify(notify, channel, payload)
        if semaphore is not None and release_user_id is not None:
            await _safe_release_semaphore(semaphore, release_user_id)


async def _drain_ingest_overdue(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    semaphore: Semaphore | None = None,
) -> None:
    """Claim + nudge/escalate + commit every currently-overdue `INGEST_PENDING` row,
    one at a time, until none remain (F2). No NOTIFY: the ingest loop's own poll
    backstop picks up the nudged `available_at` (see `jobs.watchdog.
    sweep_ingest_overdue`'s docstring).

    `semaphore` is optional/backward-compatible (issue #16 slot-leak fix): a row
    escalated to FAILED (ingest_attempts already at the ceiling) has left
    `ACTIVE_JOB_STATES`, so its slot is released, post-commit, best-effort. A nudged
    row (still INGEST_PENDING) must NOT release.
    """
    while True:
        async with sessionmaker() as session:
            job = await claim_ingest_overdue_job(session, settings)
            if job is None:
                return
            await sweep_ingest_overdue(job, settings=settings)
            await session.commit()
            release_user_id = (
                job.user_id
                if semaphore is not None and job.state == JOB_STATE_FAILED
                else None
            )

        if semaphore is not None and release_user_id is not None:
            await _safe_release_semaphore(semaphore, release_user_id)


async def _drain_terminal_failures(
    sessionmaker: async_sessionmaker[AsyncSession],
    rate_limiter: RateLimiter,
    publish_user_event: PublishUserEventFn,
) -> None:
    """Claim + stamp + commit every currently-unhandled `FAILED` row, one at a time,
    until none remain (F2). The refund + per-user notify fire only after each row's
    own `failure_handled_at` commit succeeds (F4) -- a crash between the two costs at
    most one missed refund, never a double one."""
    while True:
        async with sessionmaker() as session:
            job = await claim_terminal_failure_job(session)
            if job is None:
                return
            intent = await sweep_terminal_failures(job)
            await session.commit()

        if intent is not None:
            await _safe_refund(rate_limiter, intent.identity, intent.ip, intent.day)
            quota_refunded_total.inc()

            message = json.dumps({"event": "job-failed", "job_id": intent.job_id})
            await _safe_publish_user_event(publish_user_event, intent.user_id, message)
            user_notifications_total.labels(type="job_failed").inc()


async def _run_sweeps(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: GenerationClient,
    rate_limiter: RateLimiter,
    settings: Settings,
    notify: NotifyFn,
    publish_user_event: PublishUserEventFn,
    semaphore: Semaphore | None = None,
) -> None:
    """One tick: drain each of the four sweeps to exhaustion in turn (F2), each row's
    commit-then-best-effort-side-effect ordering handled by its own `_drain_*` helper
    (F4); then, if a `semaphore` was supplied, reconcile it from Postgres truth (Part
    B, issue #16 slot-leak fix) -- a backstop for any slot a release site above never
    got to fire for. `semaphore` is optional/backward-compatible, mirroring every
    other DI hook in this module."""
    await _drain_waiting_overdue(sessionmaker, client, settings, notify, semaphore)
    await _drain_submitting_stuck(sessionmaker, settings, notify, semaphore)
    await _drain_ingest_overdue(sessionmaker, settings, semaphore)
    await _drain_terminal_failures(sessionmaker, rate_limiter, publish_user_event)
    if semaphore is not None:
        await _safe_reconcile_semaphore(sessionmaker, semaphore)


async def run_watchdog(settings: Settings, stop: asyncio.Event) -> None:
    """Run the periodic watchdog sweep until `stop` is set."""
    sessionmaker = get_sessionmaker()
    redis = get_redis()
    rate_limiter = RateLimiter(RedisRateLimitBackend(redis), settings)
    # Issue #16 slot-leak fix: the same Redis connection built above, wired into a
    # decision-layer `Semaphore` for this loop's release sites + reconcile backstop
    # (mirrors `worker/dispatch.py::run_dispatch` / `worker/ingest.py::run_ingest`'s
    # own semaphore wiring).
    semaphore = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    # asyncpg wants a plain postgres DSN; the app-wide URL carries the `+asyncpg`
    # SQLAlchemy driver tag, which asyncpg.connect() doesn't understand (mirrors
    # `worker/dispatch.py::run_dispatch`).
    dsn = settings.database_url.replace("+asyncpg", "")

    async def _publish_user_event(user_id: str, message: str) -> None:
        await redis.publish(
            user_channel(settings.user_events_channel_prefix, user_id), message
        )

    while not stop.is_set():
        try:
            notify_conn = await asyncpg.connect(dsn)
            try:

                async def _notify(channel: str, payload: str) -> None:
                    await notify_conn.execute(
                        "SELECT pg_notify($1, $2)", channel, payload
                    )

                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    client = HttpGenerationClient(settings, http_client)
                    while not stop.is_set():
                        await _run_sweeps(
                            sessionmaker,
                            client,
                            rate_limiter,
                            settings,
                            _notify,
                            _publish_user_event,
                            semaphore,
                        )
                        try:
                            await asyncio.wait_for(
                                stop.wait(),
                                timeout=settings.watchdog_poll_interval_seconds,
                            )
                        except asyncio.TimeoutError:
                            pass  # normal sweep tick
            finally:
                await notify_conn.close()
        except Exception:
            log.exception("watchdog_loop_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.watchdog_poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass  # back off, then retry
