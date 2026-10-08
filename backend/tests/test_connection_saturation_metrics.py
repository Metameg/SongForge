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


class _FakeOrig(Exception):
    """Stands in for the asyncpg/psycopg original exception, carrying a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _operational_error(sqlstate: str) -> sqlalchemy.exc.OperationalError:
    return sqlalchemy.exc.OperationalError("stmt", {}, _FakeOrig(sqlstate))


async def _db_too_many(request: object) -> PlainTextResponse:
    raise _operational_error("53300")  # Postgres: too many clients already


async def _db_other_error(request: object) -> PlainTextResponse:
    raise _operational_error("57014")  # e.g. query canceled -- a real error, NOT overload


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = Starlette(
        routes=[
            Route("/ok", _ok),
            Route("/pool", _pool_timeout),
            Route("/boom", _boom),
            Route("/dbfull", _db_too_many),
            Route("/dberr", _db_other_error),
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


def test_pool_timeout_returns_graceful_503_with_retry_after_and_counts_it() -> None:
    client = _client(raise_server_exceptions=False)
    timeouts_before = _count(metrics.db_pool_acquire_timeouts_total)
    in_flight_before = _count(metrics.http_requests_in_flight)
    http_503_before = _count(
        metrics.http_requests_total, method="GET", path="/pool", status="503"
    )

    resp = client.get("/pool")

    # Overload is now a graceful, retryable 503 -- not a bare 500.
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")  # client is told to back off and retry
    assert (
        _count(metrics.http_requests_total, method="GET", path="/pool", status="503")
        == http_503_before + 1
    )
    assert _count(metrics.db_pool_acquire_timeouts_total) == timeouts_before + 1
    assert _count(metrics.http_requests_in_flight) == in_flight_before  # finally: dec


def test_db_too_many_connections_returns_503_and_counts_a_db_rejection() -> None:
    client = _client(raise_server_exceptions=False)
    rejected_before = _count(metrics.db_connection_rejected_total)

    resp = client.get("/dbfull")

    assert resp.status_code == 503
    assert resp.headers.get("Retry-After")
    assert _count(metrics.db_connection_rejected_total) == rejected_before + 1


def test_other_operational_error_is_a_500_not_a_graceful_503() -> None:
    client = _client(raise_server_exceptions=False)
    rejected_before = _count(metrics.db_connection_rejected_total)
    http_500_before = _count(
        metrics.http_requests_total, method="GET", path="/dberr", status="500"
    )

    assert client.get("/dberr").status_code == 500

    assert (
        _count(metrics.http_requests_total, method="GET", path="/dberr", status="500")
        == http_500_before + 1
    )
    # A non-overload operational error must NOT be masked as a graceful DB rejection.
    assert _count(metrics.db_connection_rejected_total) == rejected_before


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


def test_get_engine_honors_configured_pool_sizing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DB_POOL_SIZE / DB_MAX_OVERFLOW flow into the web engine's pool so a load test can
    widen it. We capture create_async_engine's kwargs rather than build a real engine."""
    from songforge import db

    captured: dict[str, object] = {}

    def _fake_create(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    class _Settings:
        database_url = "postgresql+asyncpg://u:p@h:5432/d"
        db_pgbouncer_transaction_mode = False
        db_pool_size = 150
        db_max_overflow = 175

    monkeypatch.setattr(db, "create_async_engine", _fake_create)
    monkeypatch.setattr(db, "get_settings", lambda: _Settings())
    db.get_engine.cache_clear()
    try:
        db.get_engine()
    finally:
        db.get_engine.cache_clear()  # don't leak the fake engine to other tests

    assert captured["pool_size"] == 150
    assert captured["max_overflow"] == 175
