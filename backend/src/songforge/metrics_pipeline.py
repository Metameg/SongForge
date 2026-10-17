"""Point-in-time pipeline gauges: jobs-by-state (Postgres) + generation-slot use (Redis).

These are fleet-wide facts, so the worker refreshes them on a timer
(:func:`refresh_pipeline_gauges`, see ``worker/pipeline_metrics.py``) rather than every web
replica recomputing them on every scrape. Both reads are bounded by in-flight work, never by
history: the job count only covers non-terminal states via a partial index, and the slot read
is a single Redis GET. Finished/failed throughput comes from the existing counters
(``songforge_ingest_completed_total``, ``songforge_jobs_failed_total``, ...).

The web tier's ``/metrics`` only calls :func:`refresh_db_pool_gauge`, which reads in-memory
pool counters, so a web scrape never touches a datastore.
"""

from __future__ import annotations

from prometheus_client import Gauge
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from songforge.config import Settings
from songforge.logging_setup import get_logger
from songforge.metrics import REGISTRY
from songforge.models import NON_TERMINAL_JOB_STATES, Job

log = get_logger(__name__)

jobs_in_state = Gauge(
    "songforge_jobs_in_state",
    "Job rows currently in each NON-TERMINAL state (QUEUED, SUBMITTING, "
    "WAITING_FOR_WEBHOOK, INGEST_PENDING), zero-filled. Refreshed by the worker. "
    "Finished/failed jobs are counters, e.g. songforge_ingest_completed_total.",
    labelnames=("state",),
    registry=REGISTRY,
)

generation_slots_in_use = Gauge(
    "songforge_generation_slots_in_use",
    "Generation semaphore slots currently held (the global Redis counter). Refreshed by "
    "the worker.",
    labelnames=("scope",),
    registry=REGISTRY,
)

generation_slots_capacity = Gauge(
    "songforge_generation_slots_capacity",
    "Configured global generation semaphore capacity, so a dashboard can compute "
    "utilization without hardcoding the cap.",
    labelnames=("scope",),
    registry=REGISTRY,
)


db_pool_connections = Gauge(
    "songforge_db_pool_connections",
    "App-side SQLAlchemy DB connection pool, point-in-time (issue #18 follow-up): "
    "scope 'in_use' = connections currently checked out, 'capacity' = pool_size + "
    "max_overflow (the hard ceiling). Per web PROCESS -- sum() across replicas for the "
    "fleet total. When 'in_use' pins at 'capacity' while songforge_http_requests_in_"
    "flight sits far higher, the app's own pool is the bottleneck blocking requests.",
    labelnames=("state",),
    registry=REGISTRY,
)


def refresh_db_pool_gauge() -> None:
    """Set ``songforge_db_pool_connections`` from the web engine's live pool counters.

    Synchronous on purpose: SQLAlchemy's pool exposes plain (non-async) ``checkedout``/
    ``size`` counters even on an ``AsyncEngine`` (via its ``sync_engine``). Skips cleanly
    for a pool that exposes no sizing (e.g. ``NullPool``), so it never raises on a
    non-pooled engine.
    """
    from songforge.db import get_engine

    pool = get_engine().sync_engine.pool
    checkedout = getattr(pool, "checkedout", None)
    size = getattr(pool, "size", None)
    if checkedout is None or size is None:
        return  # NullPool / non-sizing pool: nothing meaningful to report
    capacity = size() + getattr(pool, "_max_overflow", 0)
    db_pool_connections.labels(state="in_use").set(checkedout())
    db_pool_connections.labels(state="capacity").set(capacity)


async def refresh_job_state_gauges(session: AsyncSession) -> None:
    """Set ``songforge_jobs_in_state`` for every non-terminal state, zero-filled.

    The ``WHERE state IN (...)`` matches the partial index ``ix_jobs_state_active``, so
    the count reads only in-flight rows however large ``jobs`` grows.
    """
    result = await session.execute(
        select(Job.state, func.count())
        .where(Job.state.in_(NON_TERMINAL_JOB_STATES))
        .group_by(Job.state)
    )
    counts = dict(result.all())
    # Clear first so a state this process reported before (e.g. READY from an older
    # build) stops being exported; no await between clear and set, so no scrape sees a gap.
    jobs_in_state.clear()
    for state in NON_TERMINAL_JOB_STATES:
        jobs_in_state.labels(state=state).set(counts.get(state, 0))


async def refresh_semaphore_gauges(redis: Redis, settings: Settings) -> None:
    """Set the global generation-slot gauges from one Redis GET (no key scan)."""
    global_raw = await redis.get(settings.semaphore_global_key)
    generation_slots_in_use.labels(scope="global").set(
        int(global_raw) if global_raw is not None else 0
    )
    generation_slots_capacity.labels(scope="global").set(
        settings.global_generation_concurrency
    )


async def refresh_pipeline_gauges(
    sessionmaker: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings
) -> None:
    """Refresh both pipeline gauge families, each best-effort: a Postgres or Redis blip
    must not stop the other from refreshing, and must never raise into the caller."""
    try:
        async with sessionmaker() as session:
            await refresh_job_state_gauges(session)
    except Exception:
        log.exception("metrics_pipeline_job_state_refresh_failed")

    try:
        await refresh_semaphore_gauges(redis, settings)
    except Exception:
        log.exception("metrics_pipeline_semaphore_refresh_failed")
