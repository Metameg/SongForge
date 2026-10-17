"""FastAPI application factory.

Wires always-on observability (structured logs, correlation IDs, ``/metrics``) and the
health probes. Domain routers (radio, create, SSE) are added by later tickets; the app
is intentionally importable and testable without any datastore connection.

Issue #10 adds the per-instance pub/sub relay (``PointerBroadcaster``) and the SSE
router. The broadcaster is *constructed* here unconditionally (object construction
only, no I/O -- same posture as ``PointerCache`` above and ``Redis.from_url(...)`` in
``redis_client.py``, which doesn't connect until first use), so it is always present on
``app.state`` for dependency injection/overriding even when nothing has "started" it.
Actually *starting* its background relay task is wired through FastAPI's ``lifespan``
(the modern, non-deprecated startup/shutdown API), started on app startup and cancelled
on shutdown. Critically: every non-SSE test in this repo constructs ``TestClient(app)``
*without* the ``with``/lifespan-context form -- bare construction plus ``.get()`` calls
never trigger ``lifespan``, so the broadcaster never actually connects to Redis in those
tests. SSE-specific tests that need the relay running serve the app under a real
``uvicorn.Server`` on an ephemeral port (``tests/test_events_sse.py``) -- which runs the
``lifespan`` and, over a real socket, can consume an infinite SSE stream that a
buffering ASGI transport (``httpx.ASGITransport``) would deadlock on -- and inject a
fake Redis via the ``redis=`` keyword below.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.responses import Response

from songforge import __version__
from songforge.config import Settings, get_settings
from songforge.logging_setup import configure_logging, get_logger
from songforge.metrics import render_latest
from songforge.metrics_pipeline import refresh_db_pool_gauge
from songforge.radio.pointer_broadcaster import PointerBroadcaster
from songforge.radio.pointer_cache import PointerCache
from songforge.radio.queue_depth import QueueDepthBroadcaster
from songforge.radio.user_events import UserEventBroadcaster
from songforge.redis_client import get_redis
from songforge.web.middleware import CorrelationIdMiddleware, MetricsMiddleware
from songforge.web.routes import create, download, events, health, now_playing, queue, quota, webhook


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(settings: Settings | None = None, redis: Redis | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await app.state.pointer_broadcaster.start()
        await app.state.user_event_broadcaster.start()
        await app.state.queue_depth_broadcaster.start()
        try:
            yield
        finally:
            await app.state.queue_depth_broadcaster.stop()
            await app.state.user_event_broadcaster.stop()
            await app.state.pointer_broadcaster.stop()

    app = FastAPI(
        title="SongForge",
        version=__version__,
        summary="Synchronized, prompt-fed global radio.",
        lifespan=lifespan,
    )

    # Order matters: correlation ID is outermost so metrics/logs see it.
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        CorrelationIdMiddleware, header_name=settings.correlation_id_header
    )

    # Issue #9, design D3: one process-local pointer cache per app instance, warmed
    # from Redis and read by `GET /now-playing` (`web/routes/now_playing.py`).
    app.state.pointer_cache = PointerCache(
        ttl_seconds=settings.radio_pointer_cache_ttl_seconds
    )

    # `redis` defaults to the shared client (only connects on first use); tests inject
    # a fake via the `redis=` keyword. Resolved once so the pub/sub broadcasters below
    # and the `/metrics` route (issue #18, AC2) share the same effective client.
    effective_redis = redis if redis is not None else get_redis()

    # Issue #10, criterion #2: the per-instance pub/sub relay -> SSE fan-out.
    app.state.pointer_broadcaster = PointerBroadcaster(
        redis=effective_redis,
        channel=settings.radio_pointer_channel,
        cache=app.state.pointer_cache,
    )

    # Issue #16, criterion A4: the per-user mirror of `pointer_broadcaster` -- ONE
    # PSUBSCRIBE per app instance, fanning `job-failed` notifications out to exactly
    # the connected client(s) registered under the matching user id.
    app.state.user_event_broadcaster = UserEventBroadcaster(
        redis=effective_redis,
        channel_prefix=settings.user_events_channel_prefix,
    )

    # depth-in-SSE follow-up: the GLOBAL mirror of `pointer_broadcaster` -- ONE
    # subscription per app instance, fanning the worker's post-commit waiting-queue
    # depth out to every `/events` client as a `queue-depth` frame, coalesced to
    # <=1 per `queue_depth_coalesce_seconds` so a plain listener's "N waiting" updates
    # between advances without a `/queue` -> Postgres count.
    app.state.queue_depth_broadcaster = QueueDepthBroadcaster(
        redis=effective_redis,
        channel=settings.radio_queue_depth_channel,
        coalesce_seconds=settings.queue_depth_coalesce_seconds,
    )

    async def metrics_route() -> Response:
        """`GET /metrics`: render this process's in-memory registry. Never queries
        Postgres or Redis -- a slow datastore read here stalled one replica's scrape
        past the agent timeout and dropped it from Grafana. Fleet-wide pipeline gauges
        are refreshed by the worker instead (`worker/pipeline_metrics.py`)."""
        try:
            refresh_db_pool_gauge()
        except Exception:
            get_logger(__name__).exception("metrics_db_pool_refresh_failed")
        return render_latest()

    app.include_router(health.router)
    app.include_router(now_playing.router)
    app.include_router(create.router)
    app.include_router(quota.router)
    app.include_router(queue.router)
    app.include_router(download.router)
    app.include_router(webhook.router)
    app.include_router(events.router)
    if settings.metrics_enabled:
        app.add_api_route(
            "/metrics", metrics_route, include_in_schema=False, tags=["observability"]
        )

    get_logger(__name__).info(
        "web_app_created", environment=settings.environment, version=__version__
    )
    return app


app = create_app()
