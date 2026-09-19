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


async def test_skip_locked_with_n_concurrent_claimers_claims_each_row_once_and_skips_none(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    """Generalizes the 2-claimer test above to N=5 concurrent claimers against exactly
    5 QUEUED rows: criterion #2 requires every worker instance to be able to run
    dispatch in parallel with no double-claim AND no row left unclaimed. A 2-claimer/
    2-row test can't rule out an off-by-one that only shows up with more contenders."""
    n = 5
    ids = [f"skip-n-{i}" for i in range(n)]
    for job_id in ids:
        await _insert_queued(conn, job_id)

    connections = [
        await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
        for _ in range(n)
    ]
    try:
        transactions = [connection.transaction() for connection in connections]
        for tx in transactions:
            await tx.start()
        try:
            claimed_rows = await asyncio.gather(
                *(connection.fetchrow(_CLAIM_SQL) for connection in connections)
            )
        except BaseException:
            for tx in transactions:
                await tx.rollback()
            raise
        else:
            for tx in transactions:
                await tx.commit()

        claimed_ids = [row["job_id"] for row in claimed_rows if row is not None]
        assert len(claimed_ids) == n, "every claimer should have won exactly one row"
        assert len(set(claimed_ids)) == n, "no row was claimed by more than one claimer"
        assert set(claimed_ids) == {f"{_TEST_JOB_PREFIX}{job_id}" for job_id in ids}, (
            "no row was left unclaimed"
        )
    finally:
        for connection in connections:
            await connection.close()


async def test_backoff_requeued_job_is_not_reclaimed_before_available_at(
    conn: "asyncpg.Connection[asyncpg.Record]",
) -> None:
    """A 429/5xx/timeout requeue sets `available_at` into the future (dispatch's
    backoff -- see `songforge/jobs/dispatch.py`'s `_apply_backoff`). The claim query's
    `available_at <= now()` predicate must actually keep such a row un-claimable until
    that time passes, or dispatch would hot-loop immediately re-submitting a job that
    just failed/was rate-limited."""
    await conn.execute(
        "INSERT INTO jobs (job_id, user_id, prompt, state, attempts, available_at) "
        "VALUES ($1, 'u1', 'a test prompt', 'QUEUED', 1, now() + interval '1 hour')",
        f"{_TEST_JOB_PREFIX}not-due-yet",
    )
    await _insert_queued(conn, "ready-now")

    async with conn.transaction():
        first = await conn.fetchrow(_CLAIM_SQL)
        assert first is not None
        assert first["job_id"] == f"{_TEST_JOB_PREFIX}ready-now"  # future row skipped
        # Mimic the real claim transition (see `claim_next_job`) so this job is no
        # longer QUEUED for the second claim attempt below -- otherwise it would be
        # re-claimed again, masking whether the *future* row was correctly excluded.
        await conn.execute(
            "UPDATE jobs SET state = 'SUBMITTING' WHERE job_id = $1", first["job_id"]
        )

    # Nothing else is claimable -- the backed-off row still isn't due.
    async with conn.transaction():
        second = await conn.fetchrow(_CLAIM_SQL)
    assert second is None


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


# ── songforge.jobs.dispatch.claim_next_job / count_active_jobs (criterion #2) ─────
#
# The tests above drive the claim SQL directly via asyncpg to characterize the raw
# mechanics (FIFO seq order, SKIP LOCKED, the partial index, LISTEN/NOTIFY). These
# drive the actual Python entry points dispatch/worker code calls, against the same
# real, migrated Postgres, via a SQLAlchemy async session.


@pytest.fixture()
async def sessionmaker():  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_SETTINGS_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_claim_next_job_returns_the_oldest_queued_job_and_transitions_it(
    conn: "asyncpg.Connection[asyncpg.Record]", sessionmaker
) -> None:
    from songforge.jobs.dispatch import claim_next_job
    from songforge.models import JOB_STATE_SUBMITTING

    await _insert_queued(conn, "orm-a")
    await _insert_queued(conn, "orm-b")

    async with sessionmaker() as session:
        job = await claim_next_job(session)

    assert job is not None
    assert job.job_id == f"{_TEST_JOB_PREFIX}orm-a"
    assert job.state == JOB_STATE_SUBMITTING

    # Committed for real -- a fresh read sees SUBMITTING, not QUEUED.
    row = await conn.fetchrow(
        "SELECT state FROM jobs WHERE job_id = $1", f"{_TEST_JOB_PREFIX}orm-a"
    )
    assert row is not None
    assert row["state"] == "SUBMITTING"


async def test_claim_next_job_never_double_claims_across_concurrent_sessions(
    conn: "asyncpg.Connection[asyncpg.Record]", sessionmaker
) -> None:
    from songforge.jobs.dispatch import claim_next_job

    await _insert_queued(conn, "orm-x")
    await _insert_queued(conn, "orm-y")

    async def _claim() -> str | None:
        async with sessionmaker() as session:
            job = await claim_next_job(session)
            return job.job_id if job else None

    results = await asyncio.gather(_claim(), _claim())
    claimed = {r for r in results if r is not None}
    assert claimed == {f"{_TEST_JOB_PREFIX}orm-x", f"{_TEST_JOB_PREFIX}orm-y"}


async def test_count_active_jobs_counts_only_active_states(
    conn: "asyncpg.Connection[asyncpg.Record]", sessionmaker
) -> None:
    from songforge.jobs.dispatch import count_active_jobs

    for i, state in enumerate(
        ["QUEUED", "SUBMITTING", "WAITING_FOR_WEBHOOK", "INGEST_PENDING", "READY", "FAILED"]
    ):
        await conn.execute(
            "INSERT INTO jobs (job_id, user_id, prompt, state, attempts) "
            "VALUES ($1, 'u1', 'p', $2, 0)",
            f"{_TEST_JOB_PREFIX}state-{i}",
            state,
        )

    async with sessionmaker() as session:
        count = await count_active_jobs(session)

    # SUBMITTING + WAITING_FOR_WEBHOOK + INGEST_PENDING only.
    assert count == 3


async def test_semaphore_release_notify_wakes_a_waiting_listener(
    conn: "asyncpg.Connection[asyncpg.Record]", sessionmaker
) -> None:
    """Orchestrator directive extending criterion #5: dispatch's release path (429/
    terminal/transient) must pg_notify the semaphore-release channel, not just the
    global counter, so a dispatcher parked on a full cap wakes promptly. Proves the
    real wiring end to end: claim a job via the ORM, run dispatch_claimed_job with a
    rejecting client and a notify_release hook that issues a genuine pg_notify on its
    own connection, and assert a separate LISTENer receives it."""
    from songforge.config import Settings
    from songforge.jobs.dispatch import claim_next_job, dispatch_claimed_job
    from songforge.jobs.generation_client import GenerationRejected

    await _insert_queued(conn, "release-notify")

    settings = Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )

    class _AlwaysAcquireSemaphore:
        async def acquire(self, user_id: str) -> bool:
            return True

        async def release(self, user_id: str) -> None:
            return None

        async def reconcile_from_active_count(self, count: int) -> None:
            return None

    class _RejectingClient:
        async def create(self, *, prompt, lyrics, webhook_url):  # type: ignore[no-untyped-def]
            raise GenerationRejected(400, "bad prompt")

    notifier_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    listener_conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    received: list[str] = []
    woke = asyncio.Event()

    def _on_notify(connection: object, pid: int, channel: str, payload: str) -> None:
        received.append(payload)
        woke.set()

    try:
        await listener_conn.add_listener(settings.semaphore_release_channel, _on_notify)

        async def _notify_release(job_id: str) -> None:
            await notifier_conn.execute(
                "SELECT pg_notify($1, $2)", settings.semaphore_release_channel, job_id
            )

        async with sessionmaker() as session:
            job = await claim_next_job(session)
            assert job is not None
            await dispatch_claimed_job(
                job,
                semaphore=_AlwaysAcquireSemaphore(),  # type: ignore[arg-type]
                client=_RejectingClient(),  # type: ignore[arg-type]
                settings=settings,
                count_active_jobs=lambda: _zero(),
                notify_release=_notify_release,
            )
            await session.commit()

        try:
            await asyncio.wait_for(woke.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("listener did not wake within 5s of the release NOTIFY")
        assert received == [f"{_TEST_JOB_PREFIX}release-notify"]
    finally:
        await listener_conn.remove_listener(settings.semaphore_release_channel, _on_notify)
        await listener_conn.close()
        await notifier_conn.close()


async def _zero() -> int:
    return 0


async def test_dispatch_429_against_the_real_simulator_requeues_releases_and_notifies(
    conn: "asyncpg.Connection[asyncpg.Record]", sessionmaker
) -> None:
    """Prod-validation gap: composes the 429 path end to end -- real Postgres claim
    + `count_active_jobs`, and a REAL simulator 429 (`X-Sim-Fault: rate-limit-429`,
    `simulator/faults.py`) driven through the actual `HttpGenerationClient.create`
    production code path, not a hand-rolled fake client standing in for the 429 --
    through `dispatch_claimed_job`. Asserts the full reaction: requeued QUEUED with
    backoff, slot released + reconciled from Postgres truth, and the release NOTIFY
    fired for a real listener (mirrors what `test_semaphore_release_notify_wakes_a_
    waiting_listener` above does for the terminal-4xx branch)."""
    from datetime import datetime, timedelta, timezone

    from songforge.config import Settings
    from songforge.jobs.dispatch import claim_next_job, count_active_jobs, dispatch_claimed_job
    from songforge.jobs.generation_client import HttpGenerationClient
    from songforge.models import JOB_STATE_QUEUED
    from songforge.simulator.faults import FAULT_HEADER, Fault
    from tests.simulator_helpers import make_rig, make_sim_client

    await _insert_queued(conn, "real-429")

    settings = Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "DISPATCH_REQUEUE_BACKOFF_SECONDS": "30",
        }
    )

    class _RecordingSemaphore:
        """A fake semaphore (not real Redis -- that's exhaustively proven for real
        in `test_semaphore_integration.py`) that records acquire/release/reconcile
        calls, so this test's scope stays on composing the real simulator's 429 +
        real Postgres's `count_active_jobs` + the release NOTIFY."""

        def __init__(self) -> None:
            self.released_for: list[str] = []
            self.reconciled_with: int | None = None

        async def acquire(self, user_id: str) -> bool:
            return True

        async def release(self, user_id: str) -> None:
            self.released_for.append(user_id)

        async def reconcile_from_active_count(self, count: int) -> None:
            self.reconciled_with = count

    semaphore = _RecordingSemaphore()

    rig = make_rig()
    async with rig.client:
        faulty_client = make_sim_client(rig.app)
        faulty_client.headers[FAULT_HEADER] = Fault.RATE_LIMIT_429.value
        async with faulty_client:
            client = HttpGenerationClient(settings, faulty_client)

            notifier_conn = await asyncpg.connect(
                _ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS
            )
            listener_conn = await asyncpg.connect(
                _ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS
            )
            received: list[str] = []
            woke = asyncio.Event()

            def _on_notify(
                connection: object, pid: int, channel: str, payload: str
            ) -> None:
                received.append(payload)
                woke.set()

            try:
                await listener_conn.add_listener(
                    settings.semaphore_release_channel, _on_notify
                )

                async def _notify_release(job_id: str) -> None:
                    await notifier_conn.execute(
                        "SELECT pg_notify($1, $2)",
                        settings.semaphore_release_channel,
                        job_id,
                    )

                before = datetime.now(timezone.utc)
                async with sessionmaker() as session:
                    job = await claim_next_job(session)
                    assert job is not None

                    async def _count_active() -> int:
                        return await count_active_jobs(session)

                    handled = await dispatch_claimed_job(
                        job,
                        semaphore=semaphore,  # type: ignore[arg-type]
                        client=client,
                        settings=settings,
                        count_active_jobs=_count_active,
                        notify_release=_notify_release,
                    )
                    await session.commit()

                assert handled is True
                assert job.state == JOB_STATE_QUEUED  # requeued, not FAILED
                assert job.available_at >= before + timedelta(seconds=30)

                # Committed for real -- a fresh read agrees.
                row = await conn.fetchrow(
                    "SELECT state FROM jobs WHERE job_id = $1",
                    f"{_TEST_JOB_PREFIX}real-429",
                )
                assert row is not None
                assert row["state"] == "QUEUED"

                assert semaphore.released_for == [job.user_id]
                # Postgres truth right after the requeue: this job (already QUEUED
                # again) is the only row, so the active-state count is 0.
                assert semaphore.reconciled_with == 0

                try:
                    await asyncio.wait_for(woke.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pytest.fail("listener did not wake within 5s of the release NOTIFY")
                assert received == [f"{_TEST_JOB_PREFIX}real-429"]
            finally:
                await listener_conn.remove_listener(
                    settings.semaphore_release_channel, _on_notify
                )
                await listener_conn.close()
                await notifier_conn.close()
