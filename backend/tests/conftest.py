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
}

for _key, _value in _TEST_ENV.items():
    os.environ.setdefault(_key, _value)
