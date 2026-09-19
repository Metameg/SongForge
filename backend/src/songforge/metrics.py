"""Prometheus metrics — always exposed at ``/metrics`` in every environment (spec #77).

Metrics are organized by load driver (create-rate, generation-rate, listener count) so
capacity can be reasoned about per driver (spec #78). This scaffold ships the HTTP-request
family; later tickets add generation/ingest/radio gauges against this same registry.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

# A dedicated registry keeps app metrics isolated and import-order independent.
REGISTRY = CollectorRegistry()

http_requests_total = Counter(
    "songforge_http_requests_total",
    "Total HTTP requests handled by the web app.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "songforge_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "path"),
    registry=REGISTRY,
)

radio_advances_total = Counter(
    "songforge_radio_advances_total",
    "Radio song advances applied by the coordinator (successful version-CAS only).",
    registry=REGISTRY,
)

radio_now_playing_source_total = Counter(
    "songforge_radio_now_playing_source_total",
    "now-playing reads served, by data source (local process cache / redis / postgres).",
    labelnames=("source",),
    registry=REGISTRY,
)

# ── Generation job queue (issue #12) ─────────────────────────────────────────────
jobs_created_total = Counter(
    "songforge_jobs_created_total",
    "Generation jobs persisted as QUEUED via POST /create.",
    registry=REGISTRY,
)

jobs_dispatched_total = Counter(
    "songforge_jobs_dispatched_total",
    "Jobs successfully submitted to the generation API (state -> WAITING_FOR_WEBHOOK).",
    registry=REGISTRY,
)

jobs_failed_total = Counter(
    "songforge_jobs_failed_total",
    "Jobs marked FAILED after a terminal (non-429) rejection from the generation API.",
    registry=REGISTRY,
)

jobs_requeued_total = Counter(
    "songforge_jobs_requeued_total",
    "Jobs requeued to QUEUED after a 429 or a transient (5xx/timeout) failure.",
    labelnames=("reason",),
    registry=REGISTRY,
)

semaphore_acquire_denied_total = Counter(
    "songforge_semaphore_acquire_denied_total",
    "Generation semaphore acquires denied because the global or per-user cap was full.",
    registry=REGISTRY,
)


def render_latest() -> Response:
    """Render the registry as a Prometheus-format HTTP response.

    Served as a direct ``GET /metrics`` route (not a sub-app mount) so scrapers hitting
    ``/metrics`` get a 200 rather than a 307 redirect to ``/metrics/``.
    """
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
