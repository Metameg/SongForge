"""Worker-side watchdog loop: periodic leaderless recovery sweep (issue #16, A1-A5).

Mirrors `worker/dispatch.py` / `worker/ingest.py`'s split: a testable drain step over
`jobs.watchdog`'s claim+sweep functions, and thin I/O wiring (`run_watchdog`) that
loops on a plain interval (recovery is not latency-critical, per
`.orchestrator/CONTEXT.md` -- a simple poll loop is acceptable; no LISTEN/NOTIFY wake
is required). Like `run_dispatch`/`run_ingest`, `run_watchdog` itself has no dedicated
unit test: any error inside it is logged and backed off, never raised, so a broken
watchdog loop never takes down the worker's other supervised loops.

`_run_sweeps` drains each of the four sweeps once per tick, one short-lived claiming
session per sweep (mirrors `worker/dispatch.py::_drain_ready_jobs`'s "one session per
claimed row" convention). The two outbound NOTIFYs sweep 1 (ingest wake) and sweep 2
(new-job wake) need are wired to a direct `asyncpg` connection (mirrors
`worker/dispatch.py::run_dispatch`'s own notify connection); sweep 4's per-user
notification is wired to a plain `redis.publish` against the same channel shape
`radio.user_events.user_channel`/`UserEventBroadcaster` use.
"""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from songforge.config import Settings
from songforge.db import get_sessionmaker
from songforge.jobs.generation_client import GenerationClient, HttpGenerationClient
from songforge.jobs.watchdog import (
    NotifyFn,
    PublishUserEventFn,
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
from songforge.radio.user_events import user_channel
from songforge.redis_client import get_redis
from songforge.web.rate_limit import RateLimiter, RedisRateLimitBackend

log = get_logger(__name__)


async def _run_sweeps(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: GenerationClient,
    rate_limiter: RateLimiter,
    settings: Settings,
    notify: NotifyFn,
    publish_user_event: PublishUserEventFn,
) -> None:
    """One pass of all four sweeps, each in its own short-lived claiming session --
    mirrors `worker/dispatch.py::_drain_ready_jobs`'s "one session per claimed row"
    convention so a slow recovery action never holds a DB transaction (and the row
    lock it took to claim) open for longer than necessary.
    """
    async with sessionmaker() as session:
        job = await claim_waiting_overdue_job(session, settings)
        if job is not None:
            await sweep_waiting_overdue(
                job, client=client, settings=settings, notify_ingest=notify
            )
            await session.commit()

    async with sessionmaker() as session:
        job = await claim_submitting_stuck_job(session, settings)
        if job is not None:
            await sweep_submitting_stuck(job, settings=settings, notify_new_job=notify)
            await session.commit()

    async with sessionmaker() as session:
        job = await claim_ingest_overdue_job(session, settings)
        if job is not None:
            await sweep_ingest_overdue(job, settings=settings)
            await session.commit()

    async with sessionmaker() as session:
        job = await claim_terminal_failure_job(session)
        if job is not None:
            await sweep_terminal_failures(
                job, rate_limiter=rate_limiter, publish_user_event=publish_user_event
            )
            await session.commit()


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
