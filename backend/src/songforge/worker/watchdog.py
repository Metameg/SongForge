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
from songforge.jobs.generation_client import GenerationClient, HttpGenerationClient
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
from songforge.metrics import quota_refunded_total, user_notifications_total
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


async def _drain_waiting_overdue(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: GenerationClient,
    settings: Settings,
    notify: NotifyFn,
) -> None:
    """Claim + poll + commit every currently-overdue `WAITING_FOR_WEBHOOK` row, one at
    a time, until none remain (F2) -- mirrors `_drain_ready_jobs`'s "one short-lived
    session per claimed row" shape. A recovered row's ingest-wake NOTIFY fires only
    after that row's own commit succeeds (F4)."""
    while True:
        async with sessionmaker() as session:
            job = await claim_waiting_overdue_job(session, settings)
            if job is None:
                return
            intent = await sweep_waiting_overdue(job, client=client, settings=settings)
            await session.commit()

        if intent is not None:
            channel, payload = intent
            await _safe_notify(notify, channel, payload)


async def _drain_submitting_stuck(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    notify: NotifyFn,
) -> None:
    """Claim + requeue + commit every currently lease-expired `SUBMITTING` row, one at
    a time, until none remain (F2). The new-job-wake NOTIFY fires only after each row's
    own commit succeeds (F4)."""
    while True:
        async with sessionmaker() as session:
            job = await claim_submitting_stuck_job(session, settings)
            if job is None:
                return
            intent: NotifyIntent = await sweep_submitting_stuck(job, settings=settings)
            await session.commit()

        channel, payload = intent
        await _safe_notify(notify, channel, payload)


async def _drain_ingest_overdue(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Claim + nudge/escalate + commit every currently-overdue `INGEST_PENDING` row,
    one at a time, until none remain (F2). No NOTIFY: the ingest loop's own poll
    backstop picks up the nudged `available_at` (see `jobs.watchdog.
    sweep_ingest_overdue`'s docstring)."""
    while True:
        async with sessionmaker() as session:
            job = await claim_ingest_overdue_job(session, settings)
            if job is None:
                return
            await sweep_ingest_overdue(job, settings=settings)
            await session.commit()


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
) -> None:
    """One tick: drain each of the four sweeps to exhaustion in turn (F2), each row's
    commit-then-best-effort-side-effect ordering handled by its own `_drain_*` helper
    (F4)."""
    await _drain_waiting_overdue(sessionmaker, client, settings, notify)
    await _drain_submitting_stuck(sessionmaker, settings, notify)
    await _drain_ingest_overdue(sessionmaker, settings)
    await _drain_terminal_failures(sessionmaker, rate_limiter, publish_user_event)


async def run_watchdog(settings: Settings, stop: asyncio.Event) -> None:
    """Run the periodic watchdog sweep until `stop` is set."""
    sessionmaker = get_sessionmaker()
    redis = get_redis()
    rate_limiter = RateLimiter(RedisRateLimitBackend(redis), settings)
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
