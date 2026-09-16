"""App-edge tests: health probes, /metrics, and correlation-ID behaviour.

These drive the FastAPI app through real HTTP (the system edge, per the spec's testing
decisions) without needing any live datastore.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from songforge.web.app import create_app

client = TestClient(create_app())


def test_health_ok() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_liveness_always_ok() -> None:
    resp = client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_readiness_reports_down_datastores() -> None:
    """With no datastores reachable, readiness is 503 and names each failed check."""
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"database": False, "redis": False}


def test_metrics_endpoint_prometheus_format() -> None:
    # Generate at least one request so a counter series exists.
    client.get("/health")
    # Must answer directly at /metrics (200), not 307-redirect to /metrics/, so a
    # Prometheus scraper configured for /metrics works without following redirects.
    resp = client.get("/metrics", follow_redirects=False)
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "songforge_http_requests_total" in resp.text


def test_correlation_id_echoed_when_provided() -> None:
    resp = client.get("/health", headers={"X-Correlation-ID": "trace-abc"})
    assert resp.headers["X-Correlation-ID"] == "trace-abc"


def test_correlation_id_generated_when_absent() -> None:
    resp = client.get("/health")
    cid = resp.headers["X-Correlation-ID"]
    assert cid and len(cid) == 32  # uuid4().hex
