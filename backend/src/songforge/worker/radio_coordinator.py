"""Worker-side radio leader loop: advisory lock + clock-driven advance timer.

Satisfies issue #8 criterion #2 (the worker advances through the static library at each
boundary) end-to-end. Leadership is a single fixed Postgres advisory lock
(`settings.radio_advisory_lock_key`) held on the worker's own direct DB connection (not
the web tier's pooled one) — only the holder runs the timer. With one worker process this
lock is acquired trivially; full failover/catch-up test coverage across multiple workers
is a later ticket (see `.orchestrator/CONTEXT.md` DEFERRED list) — this module implements
lock acquisition and use, not the kill-a-process lifecycle matrix.

The pointer-mutation logic itself (`initialize_if_absent`/`advance`) lives in
`songforge.radio.coordinator` and is unit-tested there against in-memory SQLite; this
module is thin integration wiring (real Postgres advisory lock, real sleep, real Redis)
and has no dedicated unit test — resilience (log + back off, never crash the worker) is
its main correctness property.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import text

from songforge.config import Settings
from songforge.db import get_engine, get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.models import RADIO_STATE_SINGLETON_ID, RadioState
from songforge.radio.coordinator import RecentHistoryStore, advance, initialize_if_absent
from songforge.radio.history import RedisRecentHistoryStore
from songforge.redis_client import get_redis

log = get_logger(__name__)


async def _tick(settings: Settings, history: RecentHistoryStore, redis: Redis) -> float:
    """One coordinator decision: initialize, catch up past the boundary, or wait.

    Returns the number of seconds to sleep before the next tick (0 if there is more
    catch-up work to do immediately, e.g. after a long outage).

    ``redis`` is threaded into ``initialize_if_absent``/``advance`` (issue #9, design
    D2) so a genuine init/advance best-effort populates the Redis pointer key the web
    tier reads from.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        pointer = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)

        if pointer is None:
            created = await initialize_if_absent(session, history, redis=redis)
            if not created:
                log.info("radio_library_empty", retry_in=settings.radio_coordinator_backoff_seconds)
                return settings.radio_coordinator_backoff_seconds
            pointer = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)

        if pointer is None or pointer.ends_at is None:
            # Shouldn't happen (initialize_if_absent always sets ends_at on success),
            # but never spin hot on an unexpected inconsistent row.
            return settings.radio_coordinator_backoff_seconds

        now = datetime.now(timezone.utc)
        if pointer.ends_at <= now:
            await advance(session, history, expected_version=pointer.version, redis=redis)
            # Re-read: either this call advanced it, or another leader already did.
            # Either way the caller loops immediately (return 0) to re-check the new
            # boundary rather than assuming a single advance caught up a long outage.
            return 0.0

        return max((pointer.ends_at - now).total_seconds(), 0.0)


async def run_radio_coordinator(settings: Settings, stop: asyncio.Event) -> None:
    """Acquire single-leader lock, then run the clock-driven advance loop until `stop`.

    Resilient by design: any error inside the loop body is logged and backed off rather
    than raised, so a broken radio coordinator never takes down the worker's other
    supervised loops (dispatch/ingest/watchdog, added by later tickets).
    """
    redis = get_redis()
    history = RedisRecentHistoryStore(
        redis, max_len=settings.radio_recent_history_size
    )

    while not stop.is_set():
        try:
            engine = get_engine()
            async with engine.connect() as lock_conn:
                await lock_conn.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": settings.radio_advisory_lock_key},
                )
                log.info("radio_leader_acquired", key=settings.radio_advisory_lock_key)
                try:
                    while not stop.is_set():
                        sleep_for = await _tick(settings, history, redis)
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=sleep_for)
                        except asyncio.TimeoutError:
                            pass  # boundary reached (or idle backoff) — tick again
                finally:
                    await lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": settings.radio_advisory_lock_key},
                    )
                    log.info("radio_leader_released", key=settings.radio_advisory_lock_key)
        except Exception:
            log.exception("radio_coordinator_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.radio_coordinator_backoff_seconds
                )
            except asyncio.TimeoutError:
                pass  # back off, then retry lock acquisition
