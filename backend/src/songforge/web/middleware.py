"""Request middleware: correlation ID propagation + Prometheus request instrumentation."""

from __future__ import annotations

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from songforge import correlation, metrics
from songforge.logging_setup import get_logger

_log = get_logger("songforge.web.access")

# `jobs.correlation_id` is `String(64)` (see `songforge.models`) -- this cap MUST
# match that column width, or an over-length inbound header overflows the INSERT
# (issue #18 fix-pass, security finding M2).
_CORRELATION_ID_MAX_LEN = 64
# Conservative allow-list (hex/uuid plus common separators). Anything else --
# whitespace, `<>`, control characters like CRLF -- is stripped rather than
# rejected outright, so a mostly-legal caller-supplied trace id still survives.
_CORRELATION_ID_ILLEGAL_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_correlation_id(raw: str | None) -> str:
    """Bound and sanitize an inbound correlation ID before it is bound or persisted.

    Strips every character outside ``[A-Za-z0-9._-]`` and caps the result to
    ``_CORRELATION_ID_MAX_LEN``, so both the log stream and the
    ``jobs.correlation_id`` column only ever receive a validated value (issue #18
    fix-pass: security M2 bounds the DB write, L1 covers the reflected response
    header / log-injection surface). Falls back to a freshly minted id if the
    header was absent, or nothing legal survives sanitization.
    """
    if raw is not None:
        cleaned = _CORRELATION_ID_ILLEGAL_CHARS.sub("", raw)[:_CORRELATION_ID_MAX_LEN]
        if cleaned:
            return cleaned
    return uuid.uuid4().hex


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
        cid = _sanitize_correlation_id(incoming)
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
