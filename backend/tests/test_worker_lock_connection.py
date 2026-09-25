"""Worker advisory-lock connection-tuning contract (issue #17, PRD #74 + the
keepalive/`tcp_user_timeout` gap).

Defines the seam the green phase must build -- currently absent from
`songforge.db`/`songforge.config` (see `.orchestrator/CONTEXT.md` gaps #1/#2):

- `songforge.config.Settings.worker_lock_tcp_user_timeout_seconds` -- a config-driven
  float knob in the PRD's ~10-15s range (PRD: "tune TCP keepalive / tcp_user_timeout
  to ~10-15s" so a dead leader's session is detected promptly).
- `songforge.db.get_worker_lock_engine()` -- a DEDICATED `AsyncEngine` for the
  advisory-lock connection, built with `poolclass=NullPool` (PRD #74: a transaction
  pooler must never silently hold the lock connection, since PgBouncer could hand a
  new logical "connection" to a different backend session without the caller
  noticing) and `connect_args` threading the keepalive/`tcp_user_timeout` value
  through to asyncpg's `server_settings` (the seam CONTEXT.md's gap analysis calls
  out: "asyncpg accepts server_settings + tcp_user_timeout").

London-school: mocks `create_async_engine` to capture the construction call rather
than opening a real socket -- this is a unit test asserting HOW the engine is built
(a contract on inputs to a collaborator), not an integration test; no live Postgres
is needed or used here (mirrors `db.get_engine`'s own laziness -- `create_async_engine`
never connects at construction time).

Every test below is intentionally RED until the green phase adds the config field and
the `get_worker_lock_engine` function -- the failure is a clean missing-attribute
error (`AttributeError`), not a broken import or a typo in this file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from songforge import db as db_module
from songforge.config import Settings


def _settings(**env_overrides: str) -> Settings:
    env = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "S3_ENDPOINT_URL": "http://127.0.0.1:1",
        "S3_ACCESS_KEY_ID": "test",
        "S3_SECRET_ACCESS_KEY": "test-secret",
        "S3_BUCKET": "test",
    }
    env.update(env_overrides)
    return Settings(_env=env)


def _clear_lock_engine_cache() -> None:
    """`get_worker_lock_engine` doesn't exist yet -- once it does (lru_cached, mirroring
    `get_engine`), each test must clear it so a previous test's mocked
    `create_async_engine` call doesn't leak a cached fake engine into the next one."""
    cached = getattr(db_module, "get_worker_lock_engine", None)
    if cached is not None and hasattr(cached, "cache_clear"):
        cached.cache_clear()


def test_settings_exposes_a_worker_lock_tcp_user_timeout_knob_in_the_prd_range() -> None:
    """PRD: 'tune TCP keepalive / tcp_user_timeout to ~10-15s' -- must be a config
    knob, not a hardcoded magic number buried in `db.py`."""
    settings = _settings()
    value = settings.worker_lock_tcp_user_timeout_seconds
    assert 10.0 <= value <= 15.0


def test_get_worker_lock_engine_uses_nullpool_not_the_web_tiers_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD #74: the advisory-lock connection must be direct/non-pooled -- a
    transaction pooler silently handing it to a different backend connection would
    silently break leadership. Asserts the engine is constructed with
    `poolclass=NullPool`, unlike `get_engine()`'s default (pooled) engine."""
    from sqlalchemy.pool import NullPool

    captured: dict[str, Any] = {}

    def _fake_create_async_engine(url: str, **kwargs: Any) -> MagicMock:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return MagicMock(name="fake_lock_engine")

    monkeypatch.setattr(db_module, "get_settings", _settings)
    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    _clear_lock_engine_cache()

    db_module.get_worker_lock_engine()  # type: ignore[attr-defined]

    assert captured["kwargs"].get("poolclass") is NullPool


def test_get_worker_lock_engine_passes_tcp_user_timeout_through_connect_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured `worker_lock_tcp_user_timeout_seconds` must actually reach the
    asyncpg connection via SQLAlchemy `connect_args` (`server_settings`) -- a knob
    nothing reads is not a fix."""
    captured: dict[str, Any] = {}

    def _fake_create_async_engine(url: str, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return MagicMock(name="fake_lock_engine")

    settings = _settings(WORKER_LOCK_TCP_USER_TIMEOUT_SECONDS="13")
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    _clear_lock_engine_cache()

    db_module.get_worker_lock_engine()  # type: ignore[attr-defined]

    connect_args = captured["kwargs"].get("connect_args", {})
    server_settings = connect_args.get("server_settings", {})
    assert "tcp_user_timeout" in server_settings, (
        f"expected tcp_user_timeout in connect_args.server_settings, got: {connect_args!r}"
    )
    assert str(server_settings["tcp_user_timeout"]) == str(int(13 * 1000)), (
        "tcp_user_timeout must reflect the configured seconds value, in milliseconds"
    )


def test_get_worker_lock_engine_is_a_distinct_engine_from_the_pooled_get_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the lock engine must not just BE `get_engine()` under another name --
    reusing the pooled engine for the lock connection would defeat PRD #74 even if
    `NullPool`/keepalive were configured correctly elsewhere. No mocking here: both
    engines are constructed for real, which is safe -- `create_async_engine` never
    opens a socket at construction time."""
    monkeypatch.setattr(db_module, "get_settings", _settings)
    db_module.get_engine.cache_clear()
    _clear_lock_engine_cache()

    lock_engine = db_module.get_worker_lock_engine()  # type: ignore[attr-defined]
    pooled_engine = db_module.get_engine()

    assert lock_engine is not pooled_engine
