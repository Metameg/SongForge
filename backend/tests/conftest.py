"""Shared test setup.

Sets a deterministic environment before any ``songforge`` module reads it. Datastore
URLs point at a closed local port so readiness checks fail *fast* (connection refused)
rather than hanging on DNS — the app-edge tests never need a live datastore.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from songforge import db as db_module

_TEST_ENV = {
    "ENVIRONMENT": "local",
    "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "S3_ENDPOINT_URL": "http://127.0.0.1:1",
    "S3_ACCESS_KEY_ID": "test",
    "S3_SECRET_ACCESS_KEY": "test-secret",
    "S3_BUCKET": "songforge-audio-test",
    # Issue #15: the suite runs with rate-limit enforcement OFF by default, so the edge
    # tests that don't care about limits (and the pre-#15 create tests) exercise POST
    # /create with the REAL gate dependencies but never touch the (unavailable) Redis --
    # the disabled decision layer short-circuits before any backend call. The unit
    # suites that DO exercise enforcement (`test_rate_limit.py`, `test_bot_check.py`)
    # opt back in via their own `_settings(enforce_rate_limits=True)` factory default,
    # and the one edge test for the kill switch sets it explicitly.
    "ENFORCE_RATE_LIMITS": "false",
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def _reset_engine_caches() -> Iterator[None]:
    """Clear `db.get_engine`/`db.get_worker_lock_engine`'s process-wide `lru_cache`s
    after every test.

    Both getters are cached at module scope (`maxsize=1`), so any test that
    monkeypatches `create_async_engine` to capture construction kwargs (the
    established London-school pattern in this suite -- see
    `test_worker_lock_connection.py`, `test_db_url_split.py`) leaves its FAKE engine
    cached for whichever test runs next, in any file, regardless of pytest's
    collection order. That was previously harmless because the only thing ever left
    behind was a real (lazily-constructed, never-connected) `AsyncEngine` pointed at
    this file's closed test port above -- it still fails fast and correctly on first
    use. A mocked engine does not: `unittest.mock.MagicMock` auto-implements the async
    dunder methods (`__aenter__`, etc.), so a leaked mock silently "succeeds" where a
    real connection to the closed port would fail, corrupting any later test relying on
    the documented fail-fast readiness-check behavior (e.g. `test_web_app.py`'s
    `test_readiness_reports_down_datastores`). Clearing after every test -- rather than
    only before, as individual test modules already do for their own cases -- restores
    full cross-file isolation.
    """
    yield
    db_module.get_engine.cache_clear()
    cached = getattr(db_module, "get_worker_lock_engine", None)
    if cached is not None and hasattr(cached, "cache_clear"):
        cached.cache_clear()
