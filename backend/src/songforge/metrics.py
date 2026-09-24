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
    Gauge,
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
    registry=REGISTRY
)

# ── Identity, rate limiting & abuse (issue #15) — load driver: create-rate ───────
rate_limit_rejected_total = Counter(
    "songforge_rate_limit_rejected_total",
    "POST /create requests rejected by a daily-quota cap, labelled by the cap scope.",
    labelnames=("scope",),
    registry=REGISTRY,
)

bot_check_failed_total = Counter(
    "songforge_bot_check_failed_total",
    "POST /create requests rejected because the bot check failed.",
    registry=REGISTRY,
)
# ── SSE + pub/sub (issue #10) — organized by load driver: listener count ────────
#
# The direct observable proof that listener count is decoupled from datastore load:
# `sse_connected_listeners` grows with N connected clients while `radio_advances_total`
# and the pointer-cache source counters above do not, because the fan-out happens
# in-process against ONE Redis pub/sub subscription per app instance (see
# `radio/pointer_broadcaster.py`).

sse_connected_listeners = Gauge(
    "songforge_sse_connected_listeners",
    "SSE clients currently connected to this app instance's /events endpoint.",
    registry=REGISTRY,
)

radio_pointer_events_published_total = Counter(
    "songforge_radio_pointer_events_published_total",
    "Pointer changes published to Redis pub/sub by the coordinator (successful publish only).",
    registry=REGISTRY,
)

radio_pointer_events_relayed_total = Counter(
    "songforge_radio_pointer_events_relayed_total",
    "Pointer events relayed from the pub/sub subscription to a connected SSE client "
    "(grows with listener count x pushes; the fan-out itself never touches Redis/Postgres "
    "again per listener).",
    registry=REGISTRY,
)


def render_latest() -> Response:
    """Render the registry as a Prometheus-format HTTP response.

    Served as a direct ``GET /metrics`` route (not a sub-app mount) so scrapers hitting
    ``/metrics`` get a 200 rather than a 307 redirect to ``/metrics/``.
    """
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
