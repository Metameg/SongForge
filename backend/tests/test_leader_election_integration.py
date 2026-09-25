"""Postgres-only integration tests for leader election & failover (issue #17).

Advisory locks, real concurrent-session semantics, and ``LISTEN``/``NOTIFY`` are
Postgres-only -- SQLite cannot emulate ``pg_advisory_lock``/``pg_try_advisory_lock``
session behaviour (see ``.orchestrator/CONTEXT.md`` "Testing"). Mirrors
``test_jobs_queue_integration.py``'s pattern: module-scoped Postgres-reachability
probe + ``alembic upgrade head``, real ``asyncpg`` connections driving the real
behaviour, not a hand-rolled stand-in.

Maps to issue #17's acceptance criteria:

1. Mutual exclusion + automatic failover on session death --
   ``test_pg_advisory_lock_is_mutually_exclusive_between_two_connections``,
   ``test_session_death_releases_the_lock_so_a_blocked_waiter_acquires_it``.
2. Pause-tolerance (no TTL, no false failover) --
   ``test_a_stalled_leader_that_never_unlocks_keeps_the_lock_indefinitely``.
3. Idempotent version-CAS under a two-leader overlap. Unit-level coverage already
   exists in ``test_radio_coordinator.py`` (``test_advance_with_stale_version_
   updates_zero_rows``); this file adds only the missing INTEGRATION-level case:
   two REAL concurrent Postgres sessions racing the same CAS --
   ``test_two_concurrent_sessions_racing_the_same_cas_advance_exactly_once``.
4. State reconstruction / catch-up on election ("advance one, start fresh", never
   replay) -- ``test_tick_catches_up_a_long_outage_by_advancing_exactly_once_not_
   replaying``, and (mid-window resume) ``test_leader_killed_before_its_first_
   advance_survivor_resumes_from_db_state_not_from_scratch``.
5. First-boot init via the same startup path; killing the leader advances exactly
   once -- ``test_tick_initializes_on_first_boot_then_a_forced_boundary_advances_
   once_more``.
6. The lock connection's tuning actually reaches Postgres -- PRD #74 +
   "tune TCP keepalive / tcp_user_timeout to ~10-15s" --
   ``test_get_worker_lock_engine_connects_and_postgres_accepts_tcp_user_timeout``.
   ``tests/test_worker_lock_connection.py`` proves the engine is BUILT correctly
   (mocked `create_async_engine`); this proves the resulting `server_settings` are
   a real startup parameter Postgres actually accepts, not just a dict SQLAlchemy
   happens to construct without error.

Section B (PRD testing seam #3 -- process-lifecycle failover, "the one place the
edge seam can't reach"): rather than spawning a real worker subprocess (heavy/flaky
for a unit of work this small, and the orchestrator's own suite has no existing
subprocess-worker harness to build on), these two tests model the kill faithfully
with real ``asyncpg`` connections and a hard ``.close()`` as the "kill" -- exactly
what a killed process's OS-closed socket looks like to Postgres, which is the only
thing ``pg_advisory_lock`` auto-release cares about. They drive the REAL
``songforge.worker.radio_coordinator._tick`` (not a stand-in) via a monkeypatched
``get_sessionmaker`` pointed at this real Postgres, so the actual deliverable code
is what is proven, not a reimplementation of it:

- ``test_leader_killed_mid_catchup_survivor_advances_exactly_once_not_twice`` -- the
  headline composed case (failover + catch-up + exactly-once through a real kill).
- ``test_leader_killed_before_its_first_advance_survivor_resumes_from_db_state_not_
  from_scratch`` -- the complementary case: a leader that died before ever ticking;
  the survivor must resume the existing in-window song from ``radio_state``, not
  restart the window from its own boot time.

Connects to ``TEST_DATABASE_URL`` if set, else the docker-compose dev default (see
``test_jobs_queue_integration.py``). Deliberately NOT the process env
``DATABASE_URL`` (``tests/conftest.py`` points that at a closed port).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parent.parent

_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")

_CONNECT_TIMEOUT_SECONDS = 1.5
_TEST_SONG_PREFIX = "test-issue17-"

# Test-only advisory-lock keys, well clear of `settings.radio_advisory_lock_key`
# (927341) so this file never contends with a real worker that might be running
# against the same dev Postgres. Each test that needs a lock takes its own offset.
_LOCK_KEY = 5_170_017_000


def _postgres_reachable() -> bool:
    parts = urlsplit(_ASYNCPG_DSN)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_postgres() -> None:
    """Skip this whole module up front if Postgres is unreachable -- a plain TCP
    probe so every test doesn't pay its own connect-timeout on a dead host."""
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
    """Apply real Alembic migrations before any test in this module runs, so these
    tests exercise the actual `songs`/`radio_state`/`playback_queue` tables (already
    landed by issue #8/#14)."""
    env = dict(os.environ)
    env["DATABASE_URL"] = _SETTINGS_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the integration Postgres:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


@pytest.fixture()
async def conn() -> AsyncIterator["asyncpg.Connection[asyncpg.Record]"]:
    """A connection to the real target DB, with this file's own test rows cleaned up
    before and after each test. `radio_state` is a real singleton row (id=1) shared
    across the whole schema, so it MUST be cleared between tests in this module too,
    not just this file's own prefixed rows."""
    connection = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)

    async def _clean() -> None:
        await connection.execute("DELETE FROM playback_queue")
        await connection.execute("DELETE FROM radio_state WHERE id = 1")
        await connection.execute(
            "DELETE FROM songs WHERE id LIKE $1", f"{_TEST_SONG_PREFIX}%"
        )

    await _clean()
    try:
        yield connection
    finally:
        await _clean()
        await connection.close()


@pytest.fixture()
async def pg_sessionmaker():  # type: ignore[no-untyped-def]
    """A real SQLAlchemy async engine/sessionmaker against the integration Postgres --
    what `songforge.worker.radio_coordinator._tick` is monkeypatched to use instead of
    the process-wide (closed-port, per `tests/conftest.py`) cached one."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_SETTINGS_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _make_settings():  # type: ignore[no-untyped-def]
    from songforge.config import Settings

    return Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )


async def _insert_static_song(
    connection: "asyncpg.Connection[asyncpg.Record]",
    song_id: str,
    *,
    duration_seconds: int = 180,
) -> None:
    full_id = f"{_TEST_SONG_PREFIX}{song_id}"
    await connection.execute(
        "INSERT INTO songs (id, title, source, object_key, duration_seconds) "
        "VALUES ($1, $1, 'static', $2, $3)",
        full_id,
        f"audio/{full_id}.mp3",
        duration_seconds,
    )


async def _run_tick(
    monkeypatch: pytest.MonkeyPatch,
    settings,  # type: ignore[no-untyped-def]
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    history,  # type: ignore[no-untyped-def]
    redis,  # type: ignore[no-untyped-def]
) -> float:
    """Drives the REAL `worker.radio_coordinator._tick` against real Postgres, by
    monkeypatching its module-level `get_sessionmaker` (used at call time) rather than
    the process-wide `functools.lru_cache`d one -- keeps this test isolated from any
    other test module's cached (closed-port) engine."""
    from songforge.worker import radio_coordinator as worker_coordinator

    monkeypatch.setattr(worker_coordinator, "get_sessionmaker", lambda: pg_sessionmaker)
    return await worker_coordinator._tick(settings, history, redis)


# ── A: lock semantics (criteria #1, #2) ────────────────────────────────────────────


async def test_pg_advisory_lock_is_mutually_exclusive_between_two_connections() -> None:
    """Criterion #1: exactly one connection ever holds the lock at a time. Uses
    `pg_try_advisory_lock` (non-blocking) so the contention is observed deterministically
    rather than by racing a timeout."""
    lock_key = _LOCK_KEY + 1
    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        assert await conn_a.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True
        assert await conn_b.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is False

        await conn_a.fetchval("SELECT pg_advisory_unlock($1)", lock_key)

        assert await conn_b.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True
        await conn_b.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        await conn_a.close()
        await conn_b.close()


async def test_session_death_releases_the_lock_so_a_blocked_waiter_acquires_it() -> None:
    """Criterion #1: the holder's SESSION dying (not a graceful unlock) frees the lock
    -- this is the entire failover mechanism (no TTL, no heartbeat). Uses a genuinely
    BLOCKING `pg_advisory_lock` on B (not the `_try_` variant) so the takeover is
    observed as a real wake, not a poll."""
    lock_key = _LOCK_KEY + 2
    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        assert await conn_a.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True

        b_acquired = asyncio.ensure_future(
            conn_b.fetchval("SELECT pg_advisory_lock($1)", lock_key)
        )
        await asyncio.sleep(0.2)  # give B's blocking acquire time to actually start waiting
        assert not b_acquired.done(), "B should still be blocked while A's session is alive"

        await conn_a.close()  # the "kill" -- a hard session death, not pg_advisory_unlock

        try:
            await asyncio.wait_for(b_acquired, timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("B did not take over the lock within 5s of A's session dying")

        await conn_b.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        if not conn_a.is_closed():
            await conn_a.close()
        await conn_b.close()


async def test_a_stalled_leader_that_never_unlocks_keeps_the_lock_indefinitely() -> None:
    """Criterion #2: no TTL, no renewal heartbeat -- a leader that stops making
    progress (a GC pause, a slow tick) but keeps its SESSION open must NOT lose the
    lock. Holds it idle well past any plausible heartbeat/lease window and shows a
    waiter is STILL refused (no false failover)."""
    lock_key = _LOCK_KEY + 3
    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        assert await conn_a.fetchval("SELECT pg_try_advisory_lock($1)", lock_key) is True

        await asyncio.sleep(2.0)  # simulated stall

        still_refused = await conn_b.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
        assert still_refused is False, "a stalled-but-alive leader must not false-failover"

        await conn_a.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
    finally:
        await conn_a.close()
        await conn_b.close()


# ── A: idempotent CAS under real concurrency (criterion #3) ────────────────────────


async def test_two_concurrent_sessions_racing_the_same_cas_advance_exactly_once(
    conn: "asyncpg.Connection[asyncpg.Record]",
    pg_sessionmaker,  # type: ignore[no-untyped-def]
) -> None:
    """Criterion #3, integration-level: unit tests already prove the CAS predicate in
    isolation (`test_radio_coordinator.py::test_advance_with_stale_version_updates_
    zero_rows`); this proves it holds under REAL concurrent Postgres sessions -- two
    coordinators that both read `version=N` and both race the SAME boundary CAS.
    Exactly one must win; the pointer must advance by exactly 1, never 2."""
    from songforge.radio.coordinator import advance, initialize_if_absent
    from tests.test_radio_coordinator import _StubHistory

    await _insert_static_song(conn, "m")
    await _insert_static_song(conn, "n")

    async with pg_sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
    row = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
    assert row is not None
    starting_version = row["version"]

    async def _race() -> bool:
        async with pg_sessionmaker() as session:
            return await advance(session, _StubHistory(), expected_version=starting_version)

    results = await asyncio.gather(_race(), _race())

    assert sorted(results) == [False, True], "exactly one of the two concurrent CAS attempts must win"

    final = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
    assert final is not None
    assert final["version"] == starting_version + 1, "pointer must advance by exactly 1, not 2"


# ── A: state reconstruction / catch-up + first-boot (criteria #4, #5) ─────────────


async def test_tick_catches_up_a_long_outage_by_advancing_exactly_once_not_replaying(
    conn: "asyncpg.Connection[asyncpg.Record]",
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion #4: a newly-elected leader that finds an overdue pointer (a long
    outage) must advance to a FRESH window starting ~now, not try to replay every
    boundary that was missed (PRD #54 live-radio semantics: 'advance one, start
    fresh'). Seeds `ends_at` far in the past and runs a single `_tick` election
    cycle."""
    from tests.test_radio_coordinator import _FakeRedis, _StubHistory

    await _insert_static_song(conn, "a")
    await _insert_static_song(conn, "b")

    long_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    await conn.execute(
        "INSERT INTO radio_state (id, song_id, playback_id, source, started_at, "
        "ends_at, version) VALUES (1, $1, 'pb-old', 'static', $2, $3, 0)",
        f"{_TEST_SONG_PREFIX}a",
        long_ago - timedelta(minutes=3),
        long_ago,
    )

    settings = _make_settings()
    sleep_for = await _run_tick(
        monkeypatch, settings, pg_sessionmaker, _StubHistory(), _FakeRedis()
    )

    row = await conn.fetchrow(
        "SELECT version, started_at, ends_at FROM radio_state WHERE id = 1"
    )
    assert row is not None
    assert row["version"] == 1, "must advance EXACTLY once, not replay every missed boundary"
    now = datetime.now(timezone.utc)
    assert row["started_at"] > now - timedelta(seconds=10), (
        "catch-up must start a FRESH window at ~now, not backfill from the outage start"
    )
    assert sleep_for == 0.0  # `_tick`'s own "loop and re-check" contract after an advance


async def test_tick_initializes_on_first_boot_then_a_forced_boundary_advances_once_more(
    conn: "asyncpg.Connection[asyncpg.Record]",
    pg_sessionmaker,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion #5: no pointer yet -> `_tick` initializes via the SAME startup path
    (no special-case subsystem), at version 0. Then, simulating the boundary becoming
    due on a survivor's next tick (as if the leader that just initialized were
    killed), assert it advances EXACTLY once -- not zero, not twice."""
    from tests.test_radio_coordinator import _FakeRedis, _StubHistory

    await _insert_static_song(conn, "x")
    await _insert_static_song(conn, "y")

    settings = _make_settings()
    history = _StubHistory()
    redis = _FakeRedis()

    await _run_tick(monkeypatch, settings, pg_sessionmaker, history, redis)

    row = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
    assert row is not None
    assert row["version"] == 0, "first boot must initialize via the same path, at version 0"

    await conn.execute(
        "UPDATE radio_state SET ends_at = now() - interval '1 second' WHERE id = 1"
    )

    await _run_tick(monkeypatch, settings, pg_sessionmaker, history, redis)

    row_after = await conn.fetchrow("SELECT version FROM radio_state WHERE id = 1")
    assert row_after is not None
    assert row_after["version"] == 1, "exactly one advance -- no double-advance"


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
            async with engine.connect() as conn:
                acquired = await conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                )
                assert acquired.scalar() is True, (
                    "the tuned lock connection must connect and acquire normally -- "
                    "tcp_user_timeout must not be rejected as an invalid startup param"
                )
                shown = await conn.execute(text("SHOW tcp_user_timeout"))
                assert shown.scalar() == expected_ms, (
                    "Postgres must have actually APPLIED the configured tcp_user_timeout, "
                    "not merely accepted the connection"
                )
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                )
        finally:
            await engine.dispose()
    finally:
        db_module.get_settings = engine_get_settings  # type: ignore[assignment]
        db_module.get_worker_lock_engine.cache_clear()  # type: ignore[attr-defined]


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
