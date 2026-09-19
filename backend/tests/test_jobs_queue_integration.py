"""Postgres-only integration tests for the job queue (issue #12, criteria #2 + #5).

``FOR UPDATE SKIP LOCKED`` claim ordering, the partial index, the ``Identity`` `seq`
column, and ``LISTEN``/``NOTIFY`` are Postgres-only behaviours SQLite cannot emulate
(see ``.orchestrator/CONTEXT.md`` "Testing"). These tests drive real ``asyncpg``
connections directly against the REAL ``jobs`` table (not a hand-rolled stand-in), so
they stay genuinely red until Phase 3 adds Alembic migration ``0003_jobs`` (see
``models.py``'s ``Job`` docstring) matching the locked design — then green once that
migration's DDL is correct, with no changes needed here.

Module-scoped autouse fixtures: (1) skip the whole file fast if Postgres is
unreachable, (2) otherwise run ``alembic upgrade head`` against it so the tests always
see the current migration state (missing table today -> real `UndefinedTableError`,
i.e. a legitimate red failure, not a collection error; the real `jobs` table once
Phase 3 lands it).

Connects to ``TEST_DATABASE_URL`` if set, else the docker-compose dev default
(``postgresql://songforge:songforge@127.0.0.1:55432/songforge`` — see
``docker-compose.yml`` / ``.env.example``). This is deliberately NOT the process env
``DATABASE_URL``: ``tests/conftest.py`` points that at a closed port so the fast
unit/edge tests never accidentally touch a live datastore.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parent.parent

# Settings/Alembic want the `+asyncpg` driver tag; asyncpg.connect()'s own DSN doesn't
# understand it. Both point at the same server by default.
_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")

_CONNECT_TIMEOUT_SECONDS = 1.5
_NEW_JOB_CHANNEL = "new_job"
_TEST_JOB_PREFIX = "test-issue12-"


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
    """Skip this whole module up front if Postgres is unreachable — a plain TCP probe
    so every test doesn't pay its own connect-timeout on a dead host."""
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
    """Apply real Alembic migrations to the target Postgres before any test in this
    module runs, so these tests exercise the actual `jobs` table + partial index once
    Phase 3 adds migration `0003_jobs` (and legitimately fail on `UndefinedTableError`
    until then, rather than passing against a stand-in this file invented)."""
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
    """A connection to the real target DB, with this file's own test rows (identified
    by the `test-issue12-` job_id prefix) cleaned up before and after each test so runs
    don't interfere with each other or leave residue in a shared dev database."""
    connection = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)

    async def _clean() -> None:
        try:
            await connection.execute(
                "DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%"
            )
        except asyncpg.PostgresError:
            pass  # `jobs` doesn't exist yet (migration 0003 not applied) -- fine, the
            # tests below will fail red on that for the right reason.

    await _clean()
    try:
        yield connection
    finally:
        await _clean()
        await connection.close()


async def _insert_queued(
    connection: "asyncpg.Connection[asyncpg.Record]", job_id: str, user_id: str = "u1"
) -> None:
    await connection.execute(
        "INSERT INTO jobs (job_id, user_id, prompt, state, attempts) "
        "VALUES ($1, $2, 'a test prompt', 'QUEUED', 0)",
        f"{_TEST_JOB_PREFIX}{job_id}",
        user_id,
    )


_CLAIM_SQL = """
    SELECT job_id FROM jobs
    WHERE state = 'QUEUED' AND available_at <= now()
    ORDER BY seq
    FOR UPDATE SKIP LOCKED
    LIMIT 1
"""


async def test_claim_returns_jobs_in_fifo_seq_order(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    """Criterion #2: dispatch claims the oldest QUEUED job first, by `seq` (insertion
    order via the Postgres `Identity` column), not e.g. arbitrary physical row order."""
    await _insert_queued(conn, "a")
    await _insert_queued(conn, "b")
    await _insert_queued(conn, "c")

    async with conn.transaction():
        first = await conn.fetchrow(_CLAIM_SQL)
        assert first is not None
        assert first["job_id"] == f"{_TEST_JOB_PREFIX}a"
        await conn.execute(
            "UPDATE jobs SET state = 'SUBMITTING' WHERE job_id = $1", first["job_id"]
        )

    async with conn.transaction():
        second = await conn.fetchrow(_CLAIM_SQL)
        assert second is not None
        assert second["job_id"] == f"{_TEST_JOB_PREFIX}b"


async def test_skip_locked_never_double_claims_across_concurrent_connections(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    """Two dispatcher instances (separate connections/transactions) racing to claim
    from the same QUEUED set must never both claim the same row (criterion #2: every
    worker instance can run dispatch in parallel without double-claiming)."""
    await _insert_queued(conn, "x")
    await _insert_queued(conn, "y")

    conn_a = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    conn_b = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        # Both SELECTs must be in flight (locked/uncommitted on connection A) before B's
        # SELECT runs, so SKIP LOCKED is actually exercised rather than the two claims
        # happening to serialize by accident.
        tx_a = conn_a.transaction()
        tx_b = conn_b.transaction()
        await tx_a.start()
        await tx_b.start()
        try:
            claimed_a, claimed_b = await asyncio.gather(
                conn_a.fetchrow(_CLAIM_SQL), conn_b.fetchrow(_CLAIM_SQL)
            )
        except BaseException:
            await tx_a.rollback()
            await tx_b.rollback()
            raise
        else:
            await tx_a.commit()
            await tx_b.commit()

        assert claimed_a is not None
        assert claimed_b is not None
        assert claimed_a["job_id"] != claimed_b["job_id"]
        assert {claimed_a["job_id"], claimed_b["job_id"]} == {
            f"{_TEST_JOB_PREFIX}x",
            f"{_TEST_JOB_PREFIX}y",
        }
    finally:
        await conn_a.close()
        await conn_b.close()


async def test_partial_index_exists_on_seq_where_queued(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    """Criterion #2: a partial index `(seq) WHERE state='QUEUED'` — O(log n) claim, no
    full sort. Matched by definition (mentions `seq` + a `WHERE`/`QUEUED` predicate)
    rather than a specific index name, since CONTEXT.md locks the DDL shape, not a
    naming convention, for migration `0003_jobs` to choose."""
    rows = await conn.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'"
    )

    partial = [
        r
        for r in rows
        if "seq" in r["indexdef"].lower() and "where" in r["indexdef"].lower()
    ]
    assert partial, f"no partial index on jobs(seq) found; indexes were: {list(rows)}"
    assert any("queued" in r["indexdef"].lower() for r in partial)


async def test_partial_index_is_used_by_the_claim_query(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    # A single-row table gives the planner no reason to prefer an index scan over a
    # trivially cheap seq scan (both cost ~1), which would make this assertion flaky
    # regardless of whether the partial index is actually usable. Seed enough QUEUED
    # rows that "sorted top-1 via the index" is unambiguously cheaper than "seq scan +
    # sort the lot" -- the realistic shape criterion #2 (O(log n) claim) cares about.
    await conn.executemany(
        "INSERT INTO jobs (job_id, user_id, prompt, state, attempts) "
        "VALUES ($1, 'u1', 'a test prompt', 'QUEUED', 0)",
        [(f"{_TEST_JOB_PREFIX}plan-{i}",) for i in range(500)],
    )
    await conn.execute("ANALYZE jobs")

    plan_rows = await conn.fetch(f"EXPLAIN {_CLAIM_SQL}")
    plan_text = "\n".join(r[0] for r in plan_rows)

    assert "Index" in plan_text, f"claim query did not use an index; plan was:\n{plan_text}"


async def test_listen_notify_wakes_a_waiting_listener() -> None:
    """Criterion #5: new-job NOTIFY wakes a dispatch listener without polling."""
    listener_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    received: list[str] = []
    woke = asyncio.Event()

    def _on_notify(connection: object, pid: int, channel: str, payload: str) -> None:
        received.append(payload)
        woke.set()

    try:
        await listener_conn.add_listener(_NEW_JOB_CHANNEL, _on_notify)
        notifier_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
        try:
            await notifier_conn.execute(
                "SELECT pg_notify($1, $2)", _NEW_JOB_CHANNEL, f"{_TEST_JOB_PREFIX}notify"
            )
            try:
                await asyncio.wait_for(woke.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pytest.fail("listener did not wake within 5s of pg_notify")
        finally:
            await notifier_conn.close()
    finally:
        await listener_conn.remove_listener(_NEW_JOB_CHANNEL, _on_notify)
        await listener_conn.close()

    assert received == [f"{_TEST_JOB_PREFIX}notify"]
