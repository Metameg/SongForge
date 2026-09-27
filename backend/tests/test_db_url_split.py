"""Direct-vs-pooled DB URL split contract (issue #19, PRD #6, AC#2/#4).

Defines the seam the green phase must build -- currently absent from
`songforge.config`/`songforge.db`/the four worker LISTEN/NOTIFY modules (see
`.orchestrator/CONTEXT.md` section A/B):

- `songforge.config.Settings.worker_database_url` (env `WORKER_DATABASE_URL`) +
  `Settings.effective_worker_database_url` -- lets the worker's advisory-lock and
  LISTEN/NOTIFY connections target a direct URL while `database_url` (web tier)
  points at PgBouncer. Default `None` means dev/tests/single-node are unchanged
  (worker == web URL).
- `songforge.config.Settings.db_pgbouncer_transaction_mode` (env
  `DB_PGBOUNCER_TRANSACTION_MODE`) -- gates disabling asyncpg's statement cache on
  the pooled web engine, since transaction-mode PgBouncer can hand a prepared
  statement's session to a different backend.
- `songforge.db.worker_asyncpg_dsn(settings)` -- single source of truth for the raw
  `asyncpg.connect()` DSN: `effective_worker_database_url` with the `+asyncpg`
  SQLAlchemy driver tag stripped. `get_worker_lock_engine()` and all four worker
  LISTEN/NOTIFY modules (`dispatch.py`, `watchdog.py`, `radio_coordinator.py`,
  `ingest.py`) must derive from this helper instead of each inlining
  `settings.database_url.replace("+asyncpg", "")` against the WRONG (web) URL.
- `songforge.db.get_engine()` -- adds `connect_args={"statement_cache_size": 0}`
  iff `db_pgbouncer_transaction_mode` is true; omits it otherwise (default), so
  dev/tests keep asyncpg's prepared-statement cache.

London-school: mocks `create_async_engine` to capture construction kwargs (mirrors
`tests/test_worker_lock_connection.py`'s established pattern) rather than opening a
real socket; the four worker-module tests run the real `run_*` entrypoint with an
ALREADY-SET `stop` event (so the LISTEN/NOTIFY loop body never executes -- only the
pre-loop DSN computation does) and monkeypatch the module's imported
`worker_asyncpg_dsn` name to prove it's actually CALLED, not just available.

Every test below is intentionally RED until the green phase adds these fields,
this helper, and the four modules' refactor -- most fail with a clean
missing-attribute/missing-function error (`AttributeError`), not a broken import or
a typo in this file; see individual docstrings for the one case (URL-mismatch
assertions) where the failure is a value mismatch instead, because the production
code path already runs today without touching the not-yet-existent attribute.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from songforge import db as db_module
from songforge.config import Settings
from songforge.worker import dispatch as worker_dispatch_module
from songforge.worker import ingest as worker_ingest_module
from songforge.worker import radio_coordinator as worker_radio_coordinator_module
from songforge.worker import watchdog as worker_watchdog_module
from songforge.worker.dispatch import run_dispatch
from songforge.worker.ingest import run_ingest
from songforge.worker.radio_coordinator import run_radio_coordinator
from songforge.worker.watchdog import run_watchdog


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


def _clear_engine_caches() -> None:
    """Engines are `lru_cache`d (mirrors `get_engine`/`get_worker_lock_engine`) --
    clear both between cases so a previous test's mocked `create_async_engine` call
    can't leak a cached fake engine into the next one."""
    db_module.get_engine.cache_clear()
    cached = getattr(db_module, "get_worker_lock_engine", None)
    if cached is not None and hasattr(cached, "cache_clear"):
        cached.cache_clear()


# ── 1. `Settings.effective_worker_database_url` ─────────────────────────────────


def test_effective_worker_database_url_defaults_to_database_url_when_worker_url_unset() -> (
    None
):
    """Dev/tests/single-node: no `WORKER_DATABASE_URL` -> worker uses the same URL
    as the web tier, unchanged from today's behavior."""
    settings = _settings(DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:1/songforge")
    assert settings.effective_worker_database_url == settings.database_url


def test_effective_worker_database_url_returns_distinct_worker_database_url_when_set() -> (
    None
):
    """AC#2: when `WORKER_DATABASE_URL` is set (pointing directly at Postgres while
    `DATABASE_URL` points at PgBouncer), the worker's effective URL must be THAT
    distinct value, not silently fall back to the pooled web URL."""
    settings = _settings(
        DATABASE_URL="postgresql+asyncpg://u:p@pgbouncer:6432/songforge",
        WORKER_DATABASE_URL="postgresql+asyncpg://u:p@direct-pg:5432/songforge",
    )
    assert (
        settings.effective_worker_database_url
        == "postgresql+asyncpg://u:p@direct-pg:5432/songforge"
    )
    assert settings.effective_worker_database_url != settings.database_url


# ── 2. `Settings.db_pgbouncer_transaction_mode` ─────────────────────────────────


def test_db_pgbouncer_transaction_mode_defaults_to_false() -> None:
    settings = _settings()
    assert settings.db_pgbouncer_transaction_mode is False


def test_db_pgbouncer_transaction_mode_reads_env_true() -> None:
    settings = _settings(DB_PGBOUNCER_TRANSACTION_MODE="true")
    assert settings.db_pgbouncer_transaction_mode is True


# ── 3. `db.worker_asyncpg_dsn(settings)` ────────────────────────────────────────


def test_worker_asyncpg_dsn_strips_asyncpg_driver_tag_from_default_worker_url() -> None:
    """No `WORKER_DATABASE_URL` set -> derived from `database_url` (== the effective
    worker URL in the single-node/dev case), with `+asyncpg` stripped for
    `asyncpg.connect()`."""
    settings = _settings(DATABASE_URL="postgresql+asyncpg://u:p@127.0.0.1:1/songforge")

    dsn = db_module.worker_asyncpg_dsn(settings)  # type: ignore[attr-defined]

    assert dsn == "postgresql://u:p@127.0.0.1:1/songforge"
    assert "+asyncpg" not in dsn


def test_worker_asyncpg_dsn_reflects_worker_url_not_web_url_when_they_differ() -> None:
    """AC#2's core guarantee: when `WORKER_DATABASE_URL` differs from `DATABASE_URL`,
    the DSN the worker's `asyncpg.connect()` calls use must reflect the WORKER url,
    never the web/pooled one -- this is what actually bypasses PgBouncer for
    LISTEN/NOTIFY + the advisory lock."""
    settings = _settings(
        DATABASE_URL="postgresql+asyncpg://u:p@pgbouncer:6432/songforge",
        WORKER_DATABASE_URL="postgresql+asyncpg://u:p@direct-pg:5432/songforge",
    )

    dsn = db_module.worker_asyncpg_dsn(settings)  # type: ignore[attr-defined]

    assert dsn == "postgresql://u:p@direct-pg:5432/songforge"
    assert "+asyncpg" not in dsn
    assert "pgbouncer" not in dsn


# ── 5. `db.get_engine()` PgBouncer transaction-mode statement-cache safety ──────


def test_get_engine_sets_statement_cache_size_zero_when_pgbouncer_transaction_mode_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PgBouncer transaction pooling can hand a prepared statement's backend session
    to a different client mid-lifetime -- asyncpg's server-side statement cache must
    be disabled on the pooled web engine when this flag is on."""
    captured: dict[str, Any] = {}

    def _fake_create_async_engine(url: str, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return MagicMock(name="fake_pooled_engine")

    settings = _settings(DB_PGBOUNCER_TRANSACTION_MODE="true")
    # Precondition: fails here today (AttributeError) since the flag doesn't exist
    # yet -- proves this test targets the not-yet-built config knob, not just a
    # coincidental default.
    assert settings.db_pgbouncer_transaction_mode is True

    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    _clear_engine_caches()

    db_module.get_engine()

    assert captured["kwargs"].get("connect_args") == {"statement_cache_size": 0}


def test_get_engine_omits_statement_cache_size_when_pgbouncer_transaction_mode_is_false_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (single-node dev/tests, no PgBouncer): asyncpg's prepared-statement
    cache must be left alone -- disabling it unconditionally would be a needless
    perf regression for every non-PgBouncer deployment."""
    captured: dict[str, Any] = {}

    def _fake_create_async_engine(url: str, **kwargs: Any) -> MagicMock:
        captured["kwargs"] = kwargs
        return MagicMock(name="fake_pooled_engine")

    settings = _settings()
    # Precondition: fails here today (AttributeError) -- see the enabled-case test.
    assert settings.db_pgbouncer_transaction_mode is False

    monkeypatch.setattr(db_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "create_async_engine", _fake_create_async_engine)
    _clear_engine_caches()

    db_module.get_engine()

    connect_args = captured["kwargs"].get("connect_args", {})
    assert "statement_cache_size" not in connect_args


# ── 6. The four worker LISTEN/NOTIFY modules use the shared helper ─────────────
#
# Behavioral, not source-grepping: each `run_*` entrypoint is invoked for real with
# an ALREADY-SET `stop` event, so its `while not stop.is_set():` loop body never
# executes (no real `asyncpg.connect`/network I/O is attempted) -- only the
# pre-loop DSN-computation line runs. `monkeypatch.setattr(<module>,
# "worker_asyncpg_dsn", ...)` targets the name as IMPORTED INTO that worker module
# (not `songforge.db`), because that's the actual call site; today none of the four
# modules import that name at all, so `monkeypatch.setattr` itself raises a clean
# `AttributeError` immediately -- proving the module doesn't yet use the shared
# helper (still inlining its own `database_url.replace("+asyncpg", "")`).


async def test_run_dispatch_computes_its_dsn_via_the_shared_worker_asyncpg_dsn_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Settings] = []

    def _fake_dsn(settings: Settings) -> str:
        calls.append(settings)
        return "postgresql://direct-pg:5432/songforge"

    monkeypatch.setattr(worker_dispatch_module, "worker_asyncpg_dsn", _fake_dsn)

    settings = _settings()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(run_dispatch(settings, stop), timeout=5.0)

    assert calls == [settings], (
        "run_dispatch must derive its LISTEN/NOTIFY DSN via worker_asyncpg_dsn(settings)"
    )


async def test_run_watchdog_computes_its_dsn_via_the_shared_worker_asyncpg_dsn_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Settings] = []

    def _fake_dsn(settings: Settings) -> str:
        calls.append(settings)
        return "postgresql://direct-pg:5432/songforge"

    monkeypatch.setattr(worker_watchdog_module, "worker_asyncpg_dsn", _fake_dsn)

    settings = _settings()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(run_watchdog(settings, stop), timeout=5.0)

    assert calls == [settings], (
        "run_watchdog must derive its NOTIFY DSN via worker_asyncpg_dsn(settings)"
    )


async def test_run_radio_coordinator_computes_its_dsn_via_the_shared_worker_asyncpg_dsn_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Settings] = []

    def _fake_dsn(settings: Settings) -> str:
        calls.append(settings)
        return "postgresql://direct-pg:5432/songforge"

    monkeypatch.setattr(
        worker_radio_coordinator_module, "worker_asyncpg_dsn", _fake_dsn
    )

    settings = _settings()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(run_radio_coordinator(settings, stop), timeout=5.0)

    assert calls == [settings], (
        "run_radio_coordinator must derive its LISTEN DSN via worker_asyncpg_dsn(settings)"
    )


async def test_run_ingest_computes_its_dsn_via_the_shared_worker_asyncpg_dsn_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Settings] = []

    def _fake_dsn(settings: Settings) -> str:
        calls.append(settings)
        return "postgresql://direct-pg:5432/songforge"

    monkeypatch.setattr(worker_ingest_module, "worker_asyncpg_dsn", _fake_dsn)

    settings = _settings()
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(run_ingest(settings, stop), timeout=5.0)

    assert calls == [settings], (
        "run_ingest must derive its LISTEN/NOTIFY DSN via worker_asyncpg_dsn(settings)"
    )
