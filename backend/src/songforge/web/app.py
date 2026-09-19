"""FastAPI application factory.

Wires always-on observability (structured logs, correlation IDs, ``/metrics``) and the
health probes. Domain routers (radio, create, SSE) are added by later tickets; the app
is intentionally importable and testable without any datastore connection.
"""

from __future__ import annotations

from fastapi import FastAPI

from songforge import __version__
from songforge.config import Settings, get_settings
from songforge.logging_setup import configure_logging, get_logger
from songforge.metrics import render_latest
from songforge.radio.pointer_cache import PointerCache
from songforge.web.middleware import CorrelationIdMiddleware, MetricsMiddleware
from songforge.web.routes import create, health, now_playing


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level)

    app = FastAPI(
        title="SongForge",
        version=__version__,
        summary="Synchronized, prompt-fed global radio.",
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

    app.include_router(health.router)
    app.include_router(now_playing.router)
    app.include_router(create.router)
    if settings.metrics_enabled:
        app.add_api_route(
            "/metrics", render_latest, include_in_schema=False, tags=["observability"]
        )

    get_logger(__name__).info(
        "web_app_created", environment=settings.environment, version=__version__
    )
    return app


app = create_app()
