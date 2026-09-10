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
from songforge.web.middleware import CorrelationIdMiddleware, MetricsMiddleware
from songforge.web.routes import health


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

    app.include_router(health.router)
    if settings.metrics_enabled:
        app.add_api_route(
            "/metrics", render_latest, include_in_schema=False, tags=["observability"]
        )

    get_logger(__name__).info(
        "web_app_created", environment=settings.environment, version=__version__
    )
    return app


app = create_app()
