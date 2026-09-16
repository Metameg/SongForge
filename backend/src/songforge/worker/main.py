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
from songforge.worker.health import touch_heartbeat
from songforge.worker.radio_coordinator import run_radio_coordinator

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

    Later tickets add, concurrently supervised alongside these:
      * dispatch  — claim QUEUED jobs (FOR UPDATE SKIP LOCKED) + Redis semaphore
      * ingest    — download finished audio, upload to R2, enqueue the song
      * watchdog  — leaderless recovery sweep; every side-effect is row-claimed first
    """
    log.info("worker_started", environment=settings.environment)
    await asyncio.gather(
        _heartbeat_loop(settings, stop),
        run_radio_coordinator(settings, stop),
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
