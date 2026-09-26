"""Postgres-only integration tests for leader election & failover (issue #17) — part 2
of 2: the process-lifecycle failover matrix (PRD testing seam #3) and the proof that
the lock connection's ``tcp_user_timeout`` tuning actually reaches Postgres (PRD #74).
Part 1 (lock semantics, CAS, catch-up, first-boot) is in
``test_leader_election_integration.py``; the shared real-Postgres scaffolding lives in
``leader_election_helpers.py``.

Section B (PRD testing seam #3 -- process-lifecycle failover, "the one place the edge
seam can't reach"): rather than spawning a real worker subprocess (heavy/flaky for a
unit of work this small, and the suite has no existing subprocess-worker harness to
build on), these tests model the kill faithfully with real ``asyncpg`` connections and
a hard ``.close()`` as the "kill" -- exactly what a killed process's OS-closed socket
looks like to Postgres, which is the only thing ``pg_advisory_lock`` auto-release cares
about. They drive the REAL ``songforge.worker.radio_coordinator._tick`` (not a
stand-in) via a monkeypatched ``get_sessionmaker`` pointed at real Postgres, so the
actual deliverable code is proven, not a reimplementation:

- ``test_leader_killed_mid_catchup_survivor_advances_exactly_once_not_twice`` -- the
  headline composed case (failover + catch-up + exactly-once through a real kill).
- ``test_leader_killed_before_its_first_advance_survivor_resumes_from_db_state_not_
  from_scratch`` -- the complementary case (criterion #4): a leader that died before
  ever ticking; the survivor resumes the existing in-window song from ``radio_state``,
  not restart the window from its own boot time.

Section C (PRD #74 tuning reaches Postgres):
- ``test_get_worker_lock_engine_connects_and_postgres_accepts_tcp_user_timeout`` --
  ``test_worker_lock_connection.py`` proves the engine is BUILT correctly (mocked
  ``create_async_engine``); this proves the resulting ``server_settings`` are a real
  startup parameter Postgres actually accepts AND applies, not just a dict SQLAlchemy
  constructs without error.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.leader_election_helpers import (
    _ASYNCPG_DSN,
    _CONNECT_TIMEOUT_SECONDS,
    _LOCK_KEY,
    _SETTINGS_DATABASE_URL,
    _TEST_SONG_PREFIX,
    _insert_static_song,
    _make_settings,
    _migrated_schema,
    _require_postgres,
    _run_tick,
    asyncpg,
    conn,
    pg_sessionmaker,
)

pytestmark = pytest.mark.integration

# Re-export the imported fixtures so pytest resolves them by name in THIS module and,
# for the autouse ones, applies them here.
__all__ = ["_require_postgres", "_migrated_schema", "conn", "pg_sessionmaker"]


# ── B: process-lifecycle failover (PRD testing seam #3) ───────────────────────────


async def test_leader_killed_mid_catchup_survivor_advances_exactly_once_not_twice(
    conn: "asyncpg.Connection[asyncpg.Record]",
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD testing seam #3, composed: kills a real leader's Postgres SESSION mid an
    overdue-boundary catch-up, and proves the survivor that automatically acquires the
    freed advisory lock (1) takes over and (2) the station has advanced EXACTLY once
    end to end -- the fresh, fully-caught-up window is not due yet, so the survivor's
    own first tick must be a clean no-op, not a second advance."""
    from tests.test_radio_coordinator import _FakeRedis, _StubHistory

    await _insert_static_song(conn, "p")
    await _insert_static_song(conn, "q")

    long_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    await conn.execute(
        "INSERT INTO radio_state (id, song_id, playback_id, source, started_at, "
        "ends_at, version) VALUES (1, $1, 'pb-old', 'static', $2, $3, 0)",
        f"{_TEST_SONG_PREFIX}p",
        long_ago - timedelta(minutes=3),
        long_ago,
    )

    lock_key = _LOCK_KEY + 10
    settings = _make_settings()
    history = _StubHistory()
    redis = _FakeRedis()

    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        assert await conn_a.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True

        # Leader A's one and only action before it "dies": catch up the overdue
        # boundary via the real `_tick`.
        await _run_tick(monkeypatch, settings, pg_sessionmaker, history, redis)

        after_a = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
        assert after_a is not None
        assert after_a["version"] == 1  # A performed exactly one catch-up advance

        b_acquired = asyncio.ensure_future(
            conn_b.fetchval("SELECT pg_advisory_lock($1)", lock_key)
        )
        await asyncio.sleep(0.2)
        assert not b_acquired.done()

        await conn_a.close()  # the kill -- A never gets to release gracefully

        try:
            await asyncio.wait_for(b_acquired, timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("survivor did not acquire the freed lock within 5s")

        # Survivor's first tick: the fresh window A just started is NOT due yet.
        sleep_for = await _run_tick(monkeypatch, settings, pg_sessionmaker, history, redis)
        assert sleep_for > 0.0, "survivor must not double-advance a not-yet-due pointer"

        final = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
        assert final is not None
        assert final["version"] == 1, "exactly one advance across the failover, not two"

        await conn_b.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        if not conn_a.is_closed():
            await conn_a.close()
        await conn_b.close()


async def test_leader_killed_before_its_first_advance_survivor_resumes_from_db_state_not_from_scratch(
    conn: "asyncpg.Connection[asyncpg.Record]",
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion #4 + PRD seam #3: a leader that dies before ever running a tick (e.g.
    crashed the instant after winning the lock) must not cost the survivor anything --
    the survivor reconstructs the CURRENT in-window song from `radio_state`; it must
    not restart the window from its own boot time or reinitialize."""
    from tests.test_radio_coordinator import _FakeRedis, _StubHistory

    await _insert_static_song(conn, "r")

    now = datetime.now(timezone.utc)
    started_at = now - timedelta(seconds=60)
    ends_at = now + timedelta(seconds=120)
    await conn.execute(
        "INSERT INTO radio_state (id, song_id, playback_id, source, started_at, "
        "ends_at, version) VALUES (1, $1, 'pb-inwindow', 'static', $2, $3, 0)",
        f"{_TEST_SONG_PREFIX}r",
        started_at,
        ends_at,
    )

    lock_key = _LOCK_KEY + 11
    settings = _make_settings()
    history = _StubHistory()
    redis = _FakeRedis()

    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        assert await conn_a.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True
        # A dies having done NOTHING -- no tick at all.

        b_acquired = asyncio.ensure_future(
            conn_b.fetchval("SELECT pg_advisory_lock($1)", lock_key)
        )
        await asyncio.sleep(0.2)
        await conn_a.close()
        try:
            await asyncio.wait_for(b_acquired, timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("survivor did not acquire the freed lock within 5s")

        sleep_for = await _run_tick(monkeypatch, settings, pg_sessionmaker, history, redis)

        row = await conn.fetchrow(
            "SELECT version, playback_id, song_id FROM radio_state WHERE id = 1"
        )
        assert row is not None
        assert row["version"] == 0, "must resume the existing pointer, not advance/reinit"
        assert row["playback_id"] == "pb-inwindow"
        assert row["song_id"] == f"{_TEST_SONG_PREFIX}r"
        assert 100.0 <= sleep_for <= 120.0, (
            "must sleep for the REMAINING window computed from the DB's ends_at, not "
            "restart a fresh duration from the survivor's own boot time"
        )

        await conn_b.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        if not conn_a.is_closed():
            await conn_a.close()
        await conn_b.close()


# ── C: lock-connection tuning reaches real Postgres (PRD #74) ─────────────────────


async def test_get_worker_lock_engine_connects_and_postgres_accepts_tcp_user_timeout() -> None:
    """`tests/test_worker_lock_connection.py` proves `get_worker_lock_engine` is BUILT
    with `poolclass=NullPool` and `connect_args.server_settings.tcp_user_timeout` by
    mocking `create_async_engine` -- it never opens a real socket, so it can't prove
    Postgres actually ACCEPTS `tcp_user_timeout` as a startup `server_settings` param
    (some builds/poolers reject unknown startup GUCs outright). This closes that gap:
    a REAL connection, over the REAL engine, asserting both that it connects/acquires
    an advisory lock without error and that the server-side GUC was actually set to
    the configured value -- not just accepted, but applied."""
    from songforge.config import Settings
    from songforge.db import get_worker_lock_engine

    lock_key = _LOCK_KEY + 20
    settings = Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "WORKER_LOCK_TCP_USER_TIMEOUT_SECONDS": "13",
        }
    )
    expected_ms = str(int(settings.worker_lock_tcp_user_timeout_seconds * 1000))

    import songforge.db as db_module

    db_module.get_worker_lock_engine.cache_clear()  # type: ignore[attr-defined]
    engine_get_settings = db_module.get_settings
    db_module.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        engine = get_worker_lock_engine()
        try:
            async with engine.connect() as connection:
                acquired = await connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                )
                assert acquired.scalar() is True, (
                    "the tuned lock connection must connect and acquire normally -- "
                    "tcp_user_timeout must not be rejected as an invalid startup param"
                )
                shown = await connection.execute(text("SHOW tcp_user_timeout"))
                assert shown.scalar() == expected_ms, (
                    "Postgres must have actually APPLIED the configured tcp_user_timeout, "
                    "not merely accepted the connection"
                )
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )
        finally:
            await engine.dispose()
    finally:
        db_module.get_settings = engine_get_settings  # type: ignore[assignment]
        db_module.get_worker_lock_engine.cache_clear()  # type: ignore[attr-defined]
