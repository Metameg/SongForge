"""Expose the worker process's Prometheus ``REGISTRY`` over HTTP so Prometheus can
scrape it.

The worker runs a set of asyncio loops (dispatch / ingest / radio / watchdog) and NO
FastAPI app, so unlike the web tier it has no ``/metrics`` route -- which means its
counters (``songforge_jobs_dispatched_total``, ``songforge_ingest_*``,
``songforge_semaphore_released_total``, ``songforge_watchdog_recovered_total``,
``songforge_quota_refunded_total``) and its process metrics (``process_*`` from the
``ProcessCollector`` registered in ``metrics.py``) are invisible to Prometheus. This
stands up a tiny HTTP server, via ``prometheus_client``'s own threaded WSGI server (no
uvicorn/asyncio integration needed -- scrapes are infrequent and the daemon thread must
not compete with the event loop), serving the SAME shared ``REGISTRY`` the web tier
renders. A ``songforge_worker`` dns_sd scrape job discovers every worker replica on
``settings.worker_metrics_port``.

The fleet-wide pipeline gauges (``songforge_jobs_in_state``, generation slots) are
exposed only here: ``worker/pipeline_metrics.py`` refreshes them on a timer, so a scrape
of this server (like a web scrape) only renders in-memory values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import start_http_server

from songforge.config import Settings
from songforge.logging_setup import get_logger
from songforge.metrics import REGISTRY

if TYPE_CHECKING:
    from wsgiref.simple_server import WSGIServer

log = get_logger(__name__)


def start_worker_metrics_server(settings: Settings) -> WSGIServer | None:
    """Start the worker's Prometheus metrics HTTP server (a daemon thread) and return
    the server, or ``None`` when ``metrics_enabled`` is false.

    Returning the server lets the caller (and tests) shut it down deterministically;
    in the running worker it simply lives for the process's lifetime.
    """
    if not settings.metrics_enabled:
        log.info("worker_metrics_disabled")
        return None
    server, _thread = start_http_server(
        settings.worker_metrics_port, addr="0.0.0.0", registry=REGISTRY
    )
    log.info("worker_metrics_server_started", port=settings.worker_metrics_port)
    return server
