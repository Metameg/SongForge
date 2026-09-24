"""Shared test setup.

Sets a deterministic environment before any ``songforge`` module reads it. Datastore
URLs point at a closed local port so readiness checks fail *fast* (connection refused)
rather than hanging on DNS — the app-edge tests never need a live datastore.
"""

from __future__ import annotations

import os

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
