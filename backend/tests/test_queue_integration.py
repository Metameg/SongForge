"""Postgres-only integration tests for the queue status query (issue #38).

Exercises `songforge.radio.queue.get_queue_status` against REAL Postgres so FIFO order is
driven by the genuine `playback_queue.id` `Identity` sequence (SQLite cannot emulate it).
Fixtures mirror `tests/test_jobs_queue_integration.py`: a TCP-probe skip when Postgres is
unreachable, `alembic upgrade head`, and per-test cleanup of this file's own rows (by the
`test-issue38-` job/song id prefix). No new migration: `jobs` and `playback_queue` are
already on head.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import JOB_STATE_READY, Job, Song
from songforge.radio.queue import enqueue_song, get_queue_status

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parent.parent

_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")

_CONNECT_TIMEOUT_SECONDS = 1.5
_PREFIX = "test-issue38-"
_ALICE = f"{_PREFIX}alice"
_BOB = f"{_PREFIX}bob"


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
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
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


async def _clean(session: AsyncSession) -> None:
    like = f"{_PREFIX}%"
    await session.execute(
        text("DELETE FROM playback_queue WHERE song_id LIKE :p OR job_id LIKE :p"),
        {"p": like},
    )
    await session.execute(text("DELETE FROM jobs WHERE job_id LIKE :p"), {"p": like})
    await session.execute(text("DELETE FROM songs WHERE id LIKE :p"), {"p": like})
    await session.commit()


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_SETTINGS_DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        await _clean(sess)
        try:
            yield sess
        finally:
            await sess.rollback()
            await _clean(sess)
    await engine.dispose()


async def _enqueue(
    session: AsyncSession,
    name: str,
    user_id: str | None,
    *,
    played: bool = False,
) -> str:
    """Insert song + (job if user_id) + playback_queue row; the queue `id` comes from the
    real Postgres Identity sequence, so insertion order == FIFO order. Returns job_id."""
    song_id = f"{_PREFIX}song-{name}"
    job_id = f"{_PREFIX}job-{name}"
    await session.execute(
        text(
            "INSERT INTO songs (id, title, source, object_key) "
            "VALUES (:id, 't', 'generated', :key)"
        ),
        {"id": song_id, "key": f"audio/{song_id}.mp3"},
    )
    if user_id is not None:
        await session.execute(
            text(
                "INSERT INTO jobs (job_id, user_id, prompt, state, attempts) "
                "VALUES (:j, :u, 'p', 'READY', 0)"
            ),
            {"j": job_id, "u": user_id},
        )
    await session.execute(
        text(
            "INSERT INTO playback_queue (song_id, job_id, played_at) "
            "VALUES (:s, :j, CASE WHEN :played THEN now() ELSE NULL END)"
        ),
        {"s": song_id, "j": job_id if user_id is not None else None, "played": played},
    )
    await session.commit()
    return job_id


async def _own_depth_baseline(session: AsyncSession) -> int:
    """Pre-existing unplayed rows in a shared dev DB (this file cleaned its own)."""
    status = await get_queue_status(session, _ALICE)
    return status.depth


async def test_positions_follow_genuine_identity_fifo_order(session: AsyncSession) -> None:
    base = await _own_depth_baseline(session)
    a1 = await _enqueue(session, "a1", _ALICE)
    b1 = await _enqueue(session, "b1", _BOB)
    a2 = await _enqueue(session, "a2", _ALICE)

    alice = await get_queue_status(session, _ALICE)

    assert alice.depth == base + 3
    assert {p.job_id: p.position for p in alice.positions} == {
        a1: base + 1,
        a2: base + 3,
    }
    bob = await get_queue_status(session, _BOB)
    assert {p.job_id: p.position for p in bob.positions} == {b1: base + 2}


async def test_other_identity_jobs_are_excluded_from_positions(session: AsyncSession) -> None:
    await _enqueue(session, "b1", _BOB)
    a1 = await _enqueue(session, "a1", _ALICE)

    alice = await get_queue_status(session, _ALICE)

    assert [p.job_id for p in alice.positions] == [a1]


async def test_played_rows_are_excluded_from_depth_and_positions(session: AsyncSession) -> None:
    base = await _own_depth_baseline(session)
    await _enqueue(session, "aired", _ALICE, played=True)
    waiting = await _enqueue(session, "waiting", _ALICE)

    status = await get_queue_status(session, _ALICE)

    assert status.depth == base + 1
    assert [(p.job_id, p.position) for p in status.positions] == [(waiting, base + 1)]


async def test_null_job_id_row_counts_in_depth_but_not_positions(session: AsyncSession) -> None:
    base = await _own_depth_baseline(session)
    await _enqueue(session, "static", None)
    mine = await _enqueue(session, "mine", _ALICE)

    status = await get_queue_status(session, _ALICE)

    assert status.depth == base + 2
    assert [(p.job_id, p.position) for p in status.positions] == [(mine, base + 2)]


async def test_positions_via_real_enqueue_song_path(session: AsyncSession) -> None:
    """Rows written by the REAL `enqueue_song` (as `_finalize_ready` does) rank correctly."""
    base = await _own_depth_baseline(session)
    jobs: dict[str, str] = {}
    for name, user in (("r1", _BOB), ("r2", _ALICE), ("r3", _ALICE)):
        song_id, job_id = f"{_PREFIX}song-{name}", f"{_PREFIX}job-{name}"
        session.add(
            Song(id=song_id, title="t", source="generated", object_key=f"audio/{song_id}.mp3")
        )
        session.add(
            Job(job_id=job_id, user_id=user, prompt="p", state=JOB_STATE_READY, attempts=0)
        )
        await session.flush()
        await enqueue_song(session, song_id, job_id)
        await session.commit()  # commit per row so the Identity ids are strictly ordered
        jobs[name] = job_id

    alice = await get_queue_status(session, _ALICE)

    assert alice.depth == base + 3
    assert {p.job_id: p.position for p in alice.positions} == {
        jobs["r2"]: base + 2,
        jobs["r3"]: base + 3,
    }


async def test_caller_with_several_jobs_gets_each_global_rank_in_order(
    session: AsyncSession,
) -> None:
    base = await _own_depth_baseline(session)
    a1 = await _enqueue(session, "a1", _ALICE)
    await _enqueue(session, "b1", _BOB)
    await _enqueue(session, "b2", _BOB)
    a2 = await _enqueue(session, "a2", _ALICE)

    alice = await get_queue_status(session, _ALICE)

    assert [(p.job_id, p.position) for p in alice.positions] == [
        (a1, base + 1),
        (a2, base + 4),
    ]


async def test_caller_with_only_played_rows_has_no_positions(session: AsyncSession) -> None:
    await _enqueue(session, "aired1", _ALICE, played=True)
    await _enqueue(session, "aired2", _ALICE, played=True)

    status = await get_queue_status(session, _ALICE)

    assert status.positions == []
