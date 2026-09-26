"""On-scrape pipeline gauges: jobs-by-state (Postgres) + semaphore slot utilization
(Redis) -- issue #18, acceptance criterion #2.

The load-driver counters in :mod:`songforge.metrics` (``jobs_created_total``,
``semaphore_acquire_denied_total``, etc.) only fire on a state TRANSITION. "How many
jobs are in each state right now" and "how many generation slots are in use right
now" are point-in-time facts, not accumulating counters -- no ``.inc()`` call ever
fires just because a job SITS in ``WAITING_FOR_WEBHOOK``. This module reads
Postgres/Redis fresh at every scrape and sets ordinary :class:`~prometheus_client.Gauge`
objects registered on the shared :data:`songforge.metrics.REGISTRY` (async refresh
functions, not a ``Collector`` subclass -- ``Collector.collect()`` is invoked
synchronously by ``generate_latest``, and the datastores here are async; a plain
``async def`` refresh called right before rendering sidesteps any
``asyncio.run()``-inside-a-running-loop hazard).

Both refreshes are best-effort (mirrors this repo's ``_safe_release``/``_safe_notify``
convention, see ``jobs.dispatch``): a Postgres or Redis blip must never take down
``GET /metrics`` for every OTHER metric in the registry, so each refresh is wrapped in
its own try/except inside :func:`render_latest_with_pipeline_gauges` -- a failure on
one datastore still lets the other's gauges refresh, and always still returns a
renderable (if stale/zero) scrape.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from songforge.config import Settings
from songforge.logging_setup import get_logger
from songforge.metrics import REGISTRY
from songforge.models import ALL_JOB_STATES, Job

log = get_logger(__name__)

jobs_in_state = Gauge(
    "songforge_jobs_in_state",
    "Job rows currently in each state (point-in-time Postgres count, zero-filled "
    "across every state on every scrape -- issue #18, AC2).",
    labelnames=("state",),
    registry=REGISTRY,
)

generation_slots_in_use = Gauge(
    "songforge_generation_slots_in_use",
    "Generation semaphore slots currently held (point-in-time Redis read), labelled "
    "'global' (the single global counter) or 'user' (the sum across every per-user "
    "counter currently set -- never per-user-id, which would be unbounded "
    "cardinality) -- issue #18, AC2.",
    labelnames=("scope",),
    registry=REGISTRY,
)

generation_slots_capacity = Gauge(
    "songforge_generation_slots_capacity",
    "Configured generation semaphore capacity, labelled by scope, so a dashboard can "
    "compute utilization (in_use / capacity) without hardcoding the cap -- issue #18, "
    "AC2. Only 'global' is exposed: 'user' has no single meaningful capacity (it is a "
    "sum across however many distinct users currently hold a slot, not a per-user cap).",
    labelnames=("scope",),
    registry=REGISTRY,
)


async def refresh_job_state_gauges(session: AsyncSession) -> None:
    """Set ``songforge_jobs_in_state`` from a fresh ``GROUP BY state`` count.

    Zero-fills every state in :data:`songforge.models.ALL_JOB_STATES` so a state with
    no rows still renders an explicit ``0`` series -- a scraper graphing "jobs by
    state" must never see a gap just because nothing currently sits in, say,
    ``INGEST_PENDING``. Unlike a ``Counter``, a re-scrape reflects the CURRENT count,
    including a decrease (a job leaving a state), because this is a plain ``.set()``
    every time, never an accumulation.
    """
    result = await session.execute(select(Job.state, func.count()).group_by(Job.state))
    counts = dict(result.all())
    for state in ALL_JOB_STATES:
        jobs_in_state.labels(state=state).set(counts.get(state, 0))


async def refresh_semaphore_gauges(redis: Redis, settings: Settings) -> None:
    """Set ``songforge_generation_slots_in_use``/``_capacity`` from a fresh Redis read.

    ``scope="global"`` is the single global counter; ``scope="user"`` sums every
    per-user counter currently set (via ``scan_iter`` over the shared key prefix) into
    one low-cardinality series -- never a series per user id. A missing key (no slots
    held) reads as ``0``, not a missing series.
    """
    global_raw = await redis.get(settings.semaphore_global_key)
    generation_slots_in_use.labels(scope="global").set(
        int(global_raw) if global_raw is not None else 0
    )
    generation_slots_capacity.labels(scope="global").set(
        settings.global_generation_concurrency
    )

    user_total = 0
    async for key in redis.scan_iter(match=f"{settings.semaphore_user_key_prefix}*"):
        value = await redis.get(key)
        user_total += int(value) if value is not None else 0
    generation_slots_in_use.labels(scope="user").set(user_total)


async def render_latest_with_pipeline_gauges(
    session: AsyncSession, redis: Redis, settings: Settings
) -> Response:
    """Refresh the pipeline gauges (best-effort, per datastore) then render the whole
    shared registry -- the same one :func:`songforge.metrics.render_latest` serves, so
    a single ``GET /metrics`` exposes both the load-driver counters (AC1) and these
    pipeline gauges (AC2) together.

    Each refresh is isolated in its own try/except: an unreachable Postgres must not
    prevent the Redis gauges from refreshing (or vice versa), and neither may ever
    prevent this from returning a 200 with whatever static counters are already on the
    registry -- see the module docstring.
    """
    try:
        await refresh_job_state_gauges(session)
    except Exception:
        log.exception("metrics_pipeline_job_state_refresh_failed")

    try:
        await refresh_semaphore_gauges(redis, settings)
    except Exception:
        log.exception("metrics_pipeline_semaphore_refresh_failed")

    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
