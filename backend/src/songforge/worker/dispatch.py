"""Worker-side dispatch loop: LISTEN/NOTIFY wake-ups over `jobs/dispatch.dispatch_
claimed_job` (issue #12, criteria #2/#3/#5).

Wakes on either the new-job channel (`POST /create`, `web/routes/create.py`) or the
semaphore-release channel (this module's own `_notify_release`, fired by
`jobs.dispatch.dispatch_claimed_job` whenever a slot frees -- the orchestrator
directive extending criterion #5) via a direct `asyncpg` connection that bypasses any
pooler (spec #74), mirroring the radio coordinator's advisory-lock connection
(`worker/radio_coordinator.py`). The poll interval is only a backstop for a missed
notification.

Split into a testable drain step (`_drain_ready_jobs`, pure DI over its collaborators)
and thin I/O wiring (`run_dispatch`, the real LISTEN/NOTIFY connection + resilient
loop) -- mirrors `radio/coordinator.py` vs `worker/radio_coordinator.py`. Like the
radio coordinator, `run_dispatch` itself has no dedicated unit test: any error inside
it is logged and backed off, never raised, so a broken dispatch loop never takes down
the worker's other supervised loops -- resilience is its main correctness property.
"""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from songforge.config import Settings
from songforge.db import get_sessionmaker
from songforge.jobs.dispatch import (
    NotifyReleaseFn,
    claim_next_job,
    count_active_jobs,
    dispatch_claimed_job,
)
from songforge.jobs.generation_client import GenerationClient, HttpGenerationClient
from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, Semaphore
from songforge.logging_setup import get_logger
from songforge.redis_client import get_redis

log = get_logger(__name__)


async def _drain_ready_jobs(
    sessionmaker: async_sessionmaker[AsyncSession],
    semaphore: Semaphore,
    client: GenerationClient,
    settings: Settings,
    notify_release: NotifyReleaseFn,
) -> None:
    """Dispatch every currently-claimable job, one at a time, until none remain or
    the semaphore denies a slot. Each claim + dispatch + commit happens in its own
    short-lived session so a slow generation-API call never holds a DB transaction
    (and the row lock it took to claim) open for longer than necessary.
    """
    while True:
        async with sessionmaker() as session:
            job = await claim_next_job(session)
            if job is None:
                return

            async def _count_active() -> int:
                return await count_active_jobs(session)

            handled = await dispatch_claimed_job(
                job,
                semaphore=semaphore,
                client=client,
                settings=settings,
                count_active_jobs=_count_active,
                notify_release=notify_release,
            )
            await session.commit()

        if not handled:
            # No slot was available -- claim_next_job will likely hand back the same
            # contention immediately; stop this pass rather than hot-looping. The
            # next new-job or semaphore-release NOTIFY (or the poll backstop) wakes
            # another pass.
            #
            # Accepted v1 trade-off (PRD v1-FIFO, spec #61-63; quality report MED
            # finding): because claiming is strict seq-order FIFO (criterion #2), a
            # per-user-capped job at the head of the queue stops this whole drain
            # pass -- even if a different, non-saturated user's job is next in line
            # with spare global capacity. Deliberate, not a bug; a fairness/skip-
            # ahead pass is a later issue's concern.
            return


async def _wait_any(stop: asyncio.Event, wake: asyncio.Event) -> None:
    stop_task = asyncio.ensure_future(stop.wait())
    wake_task = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait([stop_task, wake_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, wake_task):
            if not task.done():
                task.cancel()


async def run_dispatch(settings: Settings, stop: asyncio.Event) -> None:
    """Run the LISTEN/NOTIFY-driven dispatch loop until `stop` is set."""
    sessionmaker = get_sessionmaker()
    redis = get_redis()
    semaphore = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    # asyncpg wants a plain postgres DSN; the app-wide URL carries the `+asyncpg`
    # SQLAlchemy driver tag, which asyncpg.connect() doesn't understand.
    dsn = settings.database_url.replace("+asyncpg", "")

    while not stop.is_set():
        try:
            listen_conn = await asyncpg.connect(dsn)
            try:
                notify_conn = await asyncpg.connect(dsn)
                try:
                    wake = asyncio.Event()

                    def _on_wake(*_args: object) -> None:
                        wake.set()

                    await listen_conn.add_listener(settings.jobs_new_channel, _on_wake)
                    await listen_conn.add_listener(
                        settings.semaphore_release_channel, _on_wake
                    )
                    log.info(
                        "dispatch_listening",
                        channels=[
                            settings.jobs_new_channel,
                            settings.semaphore_release_channel,
                        ],
                    )

                    async def _notify_release(job_id: str) -> None:
                        await notify_conn.execute(
                            "SELECT pg_notify($1, $2)",
                            settings.semaphore_release_channel,
                            job_id,
                        )

                    async with httpx.AsyncClient(timeout=30.0) as http_client:
                        client = HttpGenerationClient(settings, http_client)
                        while not stop.is_set():
                            await _drain_ready_jobs(
                                sessionmaker, semaphore, client, settings, _notify_release
                            )
                            wake.clear()
                            try:
                                await asyncio.wait_for(
                                    _wait_any(stop, wake),
                                    timeout=settings.dispatch_poll_backstop_seconds,
                                )
                            except asyncio.TimeoutError:
                                pass  # backstop poll tick
                finally:
                    await notify_conn.close()
            finally:
                await listen_conn.close()
        except Exception:
            log.exception("dispatch_loop_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.dispatch_poll_backstop_seconds
                )
            except asyncio.TimeoutError:
                pass  # back off, then retry the LISTEN connection
