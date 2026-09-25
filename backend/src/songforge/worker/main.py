"""Worker entrypoint: ``python -m songforge.worker`` (or ``songforge-worker``).

Scaffold behaviour: configure logging, then run a supervised heartbeat loop. The real
loops slot in at the seams below (each is a later ticket). The organizing rule to keep
visible: *row-claimable work scales via ``SKIP LOCKED``; only the time-triggered radio
advance needs leader election.*
"""

from __future__ import annotations

import asyncio
import signal

from songforge.config import Settings, get_settings
from songforge.logging_setup import configure_logging, get_logger
from songforge.worker.dispatch import run_dispatch
from songforge.worker.health import touch_heartbeat
from songforge.worker.ingest import run_ingest
from songforge.worker.radio_coordinator import run_radio_coordinator
from songforge.worker.watchdog import run_watchdog

log = get_logger(__name__)


async def _heartbeat_loop(settings: Settings, stop: asyncio.Event) -> None:
    """Touch the liveness heartbeat file on every tick until ``stop`` is set."""
    while not stop.is_set():
        touch_heartbeat(settings.worker_heartbeat_path)
        log.debug("worker_heartbeat")
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=settings.worker_loop_interval_seconds
            )
        except asyncio.TimeoutError:
            pass  # normal loop tick


async def run(settings: Settings, *, stop: asyncio.Event) -> None:
    """Supervise the worker loops until ``stop`` is set.

    Concurrently supervised here:
      * heartbeat — liveness file the compose healthcheck reads.
      * radio     — single-leader coordinator (pg advisory lock) owning the advance
        timer (issue #8, criterion #2).
      * dispatch  — claim QUEUED jobs (FOR UPDATE SKIP LOCKED) + Redis semaphore,
        woken by LISTEN/NOTIFY on the new-job and semaphore-release channels
        (issue #12, criteria #2/#3/#5).
      * ingest    — claim INGEST_PENDING jobs (FOR UPDATE SKIP LOCKED), download
        finished audio (refreshing an expired URL via by-id lookup when needed),
        upload to R2, and land the job at READY with a playable Song row (issue
        #13, criteria #2/#3/#4). Row-claimable like dispatch, not leader-elected.
      * watchdog  — leaderless recovery sweep; every side-effect is row-claimed first
        (issue #16, criteria A1-A5): polls overdue WAITING_FOR_WEBHOOK jobs, requeues
        crashed-mid-submit jobs, nudges/escalates stalled INGEST_PENDING jobs, and
        centralizes the terminal-failure refund + per-user notify.
    """
    log.info("worker_started", environment=settings.environment)
    await asyncio.gather(
        _heartbeat_loop(settings, stop),
        run_radio_coordinator(settings, stop),
        run_dispatch(settings, stop),
        run_ingest(settings, stop),
        run_watchdog(settings, stop),
    )
    log.info("worker_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level)

    async def _amain() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(settings, stop=stop)

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
