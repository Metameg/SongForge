"""Connection-saturation observability (issue #18 follow-up).

Two things this proves:
  1. MetricsMiddleware now counts FAILED requests (500s), including the DB-pool-timeout
     500s that were previously invisible (recorded only after a successful call_next),
     tracks in-flight requests, and flags pool-acquire timeouts specifically.
  2. metrics_pipeline.refresh_db_pool_gauge reports the app DB pool's in-use count and
     capacity (pool_size + max_overflow) from the live engine pool.
"""

from __future__ import annotations

import sqlalchemy.exc
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from songforge import metrics, metrics_pipeline
from songforge.web.middleware import MetricsMiddleware


async def _ok(request: object) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _pool_timeout(request: object) -> PlainTextResponse:
    raise sqlalchemy.exc.TimeoutError("QueuePool limit reached, connection timed out")


async def _boom(request: object) -> PlainTextResponse:
    raise RuntimeError("something else blew up")


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = Starlette(
        routes=[
            Route("/ok", _ok),
            Route("/pool", _pool_timeout),
            Route("/boom", _boom),
        ]
    )
    app.add_middleware(MetricsMiddleware)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _count(metric: object, **labels: str) -> float:
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


def test_ok_request_counts_200_and_balances_in_flight() -> None:
    client = _client()
    in_flight_before = _count(metrics.http_requests_in_flight)
    total_before = _count(
        metrics.http_requests_total, method="GET", path="/ok", status="200"
    )

    assert client.get("/ok").status_code == 200

    assert _count(metrics.http_requests_in_flight) == in_flight_before  # inc then dec
    assert (
        _count(metrics.http_requests_total, method="GET", path="/ok", status="200")
        == total_before + 1
    )


def test_pool_timeout_counts_500_the_timeout_and_balances_in_flight() -> None:
    client = _client(raise_server_exceptions=False)
    timeouts_before = _count(metrics.db_pool_acquire_timeouts_total)
    in_flight_before = _count(metrics.http_requests_in_flight)
    http_500_before = _count(
        metrics.http_requests_total, method="GET", path="/pool", status="500"
    )

    assert client.get("/pool").status_code == 500

    # The pool-timeout 500 is now COUNTED (it used to be invisible to the app).
    assert (
        _count(metrics.http_requests_total, method="GET", path="/pool", status="500")
        == http_500_before + 1
    )
    assert _count(metrics.db_pool_acquire_timeouts_total) == timeouts_before + 1
    assert _count(metrics.http_requests_in_flight) == in_flight_before  # finally: dec


def test_generic_500_is_counted_but_not_as_a_pool_timeout() -> None:
    client = _client(raise_server_exceptions=False)
    timeouts_before = _count(metrics.db_pool_acquire_timeouts_total)
    http_500_before = _count(
        metrics.http_requests_total, method="GET", path="/boom", status="500"
    )

    assert client.get("/boom").status_code == 500

    assert (
        _count(metrics.http_requests_total, method="GET", path="/boom", status="500")
        == http_500_before + 1
    )
    # A non-pool error must NOT inflate the pool-timeout counter.
    assert _count(metrics.db_pool_acquire_timeouts_total) == timeouts_before


class _FakePool:
    _max_overflow = 10

    def checkedout(self) -> int:
        return 7

    def size(self) -> int:
        return 5


class _FakeSyncEngine:
    pool = _FakePool()


class _FakeEngine:
    sync_engine = _FakeSyncEngine()


def test_refresh_db_pool_gauge_reports_in_use_and_capacity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("songforge.db.get_engine", lambda: _FakeEngine())

    metrics_pipeline.refresh_db_pool_gauge()

    assert _count(metrics_pipeline.db_pool_connections, state="in_use") == 7
    assert _count(metrics_pipeline.db_pool_connections, state="capacity") == 15  # 5 + 10


def test_refresh_db_pool_gauge_skips_a_non_sizing_pool(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class _Nullish:
        pass

    class _Sync:
        pool = _Nullish()

    class _Engine:
        sync_engine = _Sync()

    monkeypatch.setattr("songforge.db.get_engine", lambda: _Engine())
    # Must not raise on a pool that exposes no checkedout()/size() (e.g. NullPool).
    metrics_pipeline.refresh_db_pool_gauge()
