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

# ── Webhook + async ingest (issue #13) ───────────────────────────────────────────
webhooks_received_total = Counter(
    "songforge_webhooks_received_total",
    "Generation-API webhook deliveries received, by outcome.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

ingest_completed_total = Counter(
    "songforge_ingest_completed_total",
    "Jobs that reached READY via the async ingest worker (audio stored in R2).",
    registry=REGISTRY,
)

ingest_failed_total = Counter(
    "songforge_ingest_failed_total",
    "Jobs marked FAILED after ingest attempts were exhausted (or an unrecoverable "
    "data-integrity condition, e.g. a missing conversion_id_1).",
    registry=REGISTRY,
)

ingest_requeued_total = Counter(
    "songforge_ingest_requeued_total",
    "Ingest attempts requeued to INGEST_PENDING after a download/by-id failure that "
    "the in-claim refresh-and-retry didn't resolve.",
    registry=REGISTRY,
)

# ── User-song queue: fallback + interrupts (issue #14) ───────────────────────────
playback_queue_enqueued_total = Counter(
    "songforge_playback_queue_enqueued_total",
    "READY user songs enqueued onto the authoritative playback queue (criterion #1).",
    registry=REGISTRY,
)

radio_interrupts_total = Counter(
    "songforge_radio_interrupts_total",
    "Off-boundary interrupt-advances that replaced a static filler with a fresh user "
    "song (criterion #3).",
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


# ── Failure recovery: watchdog, idempotency & refund (issue #16) ─────────────────
watchdog_recovered_total = Counter(
    "songforge_watchdog_recovered_total",
    "Jobs recovered by the watchdog's periodic sweep, labelled by recovery path.",
    labelnames=("path",),
    registry=REGISTRY,
)

quota_refunded_total = Counter(
    "songforge_quota_refunded_total",
    "Daily-quota slots refunded by the watchdog's terminal-failure sweep (A4).",
    registry=REGISTRY,
)

user_notifications_total = Counter(
    "songforge_user_notifications_total",
    "Per-user SSE notifications published, labelled by notification type.",
    labelnames=("type",),
    registry=REGISTRY,
)

user_notifications_relayed_total = Counter(
    "songforge_user_notifications_relayed_total",
    "Per-user notifications relayed from the pub/sub subscription to a connected SSE "
    "client (delivery-side counterpart to user_notifications_total's publish-side "
    "count -- mirrors radio_pointer_events_relayed_total for UserEventBroadcaster).",
    registry=REGISTRY,
)

# ── Generation semaphore slot-leak fix (issue #16 follow-up) ─────────────────────
#
# `jobs.dispatch.dispatch_claimed_job` already releases on its own FAILED/429/
# transient paths (unlabelled, pre-existing) -- this covers the release sites this
# fix ADDS: every ACTIVE_JOB_STATES -> terminal transition that happens OUTSIDE
# dispatch (ingest completion/failure, a webhook failure, and the watchdog's own
# sweeps).

semaphore_released_total = Counter(
    "songforge_semaphore_released_total",
    "Generation semaphore slots released outside jobs.dispatch, by the site that "
    "released them.",
    labelnames=("site",),
    registry=REGISTRY,
)


def render_latest() -> Response:
    """Render the registry as a Prometheus-format HTTP response.

    Served as a direct ``GET /metrics`` route (not a sub-app mount) so scrapers hitting
    ``/metrics`` get a 200 rather than a 307 redirect to ``/metrics/``.
    """
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
