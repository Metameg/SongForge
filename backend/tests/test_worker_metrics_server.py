"""Unit tests for the worker's Prometheus metrics HTTP server.

The worker runs no FastAPI app, so these pin that `start_worker_metrics_server` actually
stands up a scrapable `/metrics` serving the shared REGISTRY (and respects the
`metrics_enabled` kill switch) -- the thing that makes the worker's counters visible to
Prometheus. Driven against a real ephemeral-port server (port 0) so no fixed port can
collide, torn down deterministically.
"""

from __future__ import annotations

import urllib.request
from types import SimpleNamespace
from typing import cast

from songforge.config import Settings
from songforge.worker.metrics_server import start_worker_metrics_server


def _settings(*, enabled: bool, port: int) -> Settings:
    # The helper only reads `metrics_enabled` / `worker_metrics_port`; a light stub keeps
    # the test hermetic (no DATABASE_URL/REDIS_URL env needed just to bind a socket).
    return cast(Settings, SimpleNamespace(metrics_enabled=enabled, worker_metrics_port=port))


def test_disabled_returns_none_and_binds_nothing() -> None:
    assert start_worker_metrics_server(_settings(enabled=False, port=0)) is None


def test_serves_the_shared_registry_over_http() -> None:
    server = start_worker_metrics_server(_settings(enabled=True, port=0))
    assert server is not None
    try:
        port = server.server_address[1]  # port 0 -> OS-assigned actual port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
            assert resp.status == 200
            body = resp.read().decode()
        # The shared REGISTRY carries the app's own metrics namespace + the process
        # collector -- proving this exposes the SAME registry the web tier renders.
        assert "songforge_" in body
        assert "process_cpu_seconds_total" in body
    finally:
        server.shutdown()
        server.server_close()
