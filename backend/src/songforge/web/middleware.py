"""Request middleware: correlation ID propagation + Prometheus request instrumentation."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from songforge import correlation, metrics
from songforge.logging_setup import get_logger

_log = get_logger("songforge.web.access")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation ID for the request and echo it back on the response.

    Honours an inbound correlation-ID header when present (so a caller can thread its
    own trace), otherwise mints a fresh one. Every log line emitted while handling the
    request carries it (see :mod:`songforge.correlation`).
    """

    def __init__(self, app: object, header_name: str = "X-Correlation-ID") -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.header_name = header_name

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get(self.header_name)
        cid = incoming or uuid.uuid4().hex
        token = correlation.set_correlation_id(cid)
        try:
            response = await call_next(request)
        finally:
            correlation.reset_correlation_id(token)
        response.headers[self.header_name] = cid
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Count and time every request, labelled by method, route template, and status.

    The route *template* (e.g. ``/songs/{song_id}``) is used rather than the concrete
    path to keep metric cardinality bounded.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start

        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        method = request.method

        metrics.http_request_duration_seconds.labels(method=method, path=path).observe(
            elapsed
        )
        metrics.http_requests_total.labels(
            method=method, path=path, status=str(response.status_code)
        ).inc()

        # One structured JSON access line per request, carrying the correlation ID
        # (bound by CorrelationIdMiddleware, which is outermost) — this is the log that
        # makes a request's journey traceable.
        _log.info(
            "http_request",
            method=method,
            path=path,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 2),
        )
        return response
