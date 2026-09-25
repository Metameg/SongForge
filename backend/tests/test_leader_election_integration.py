"""Postgres-only integration tests for leader election & failover (issue #17) — part 1
of 2: lock semantics, idempotent CAS under real concurrency, and state
reconstruction / catch-up / first-boot. The process-lifecycle failover matrix (PRD
testing seam #3) and the lock-connection tuning proof live in
``test_leader_election_failover_integration.py``; the shared real-Postgres scaffolding
(reachability probe, migration fixture, ``conn``/``pg_sessionmaker`` fixtures, helpers)
lives in ``leader_election_helpers.py``.

Advisory locks, real concurrent-session semantics, and ``LISTEN``/``NOTIFY`` are
Postgres-only -- SQLite cannot emulate ``pg_advisory_lock``/``pg_try_advisory_lock``
session behaviour (see ``.orchestrator/CONTEXT.md`` "Testing"). These drive real
``asyncpg`` connections and the real ``worker.radio_coordinator._tick``, not a
hand-rolled stand-in.

Maps to issue #17's acceptance criteria (this file):

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
   replaying``.
5. First-boot init via the same startup path; killing the leader advances exactly
   once -- ``test_tick_initializes_on_first_boot_then_a_forced_boundary_advances_
   once_more``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.leader_election_helpers import (
    _ASYNCPG_DSN,
    _CONNECT_TIMEOUT_SECONDS,
    _LOCK_KEY,
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
# for the autouse ones, applies them here. (Referenced so linters don't flag them as
# unused imports — they are used, as fixtures, by name.)
__all__ = ["_require_postgres", "_migrated_schema", "conn", "pg_sessionmaker"]


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
