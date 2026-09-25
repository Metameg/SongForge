"""New `jobs` columns for issue #13 (webhook metadata + song link + ingest
bookkeeping) round-trip correctly, and the `song_id` FK -> `songs.id` is portable to
SQLite (this repo's models must unit-test against in-memory SQLite -- see
`.orchestrator/CONTEXT.md` "Portable types only"). `Job.seq` (a Postgres `Identity`)
uses the same test-only `before_insert` shim as `tests/test_create_route.py`.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    SOURCE_GENERATED,
    Base,
    Job,
    Song,
)

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Any:
    event.listen(Job, "before_insert", _assign_test_seq)
    yield
    event.remove(Job, "before_insert", _assign_test_seq)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_job_ingest_columns_round_trip_with_defaults(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    job = Job(
        job_id="job-1",
        user_id="user-1",
        prompt="a test prompt",
        state=JOB_STATE_INGEST_PENDING,
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
    )
    async with sessionmaker() as session:
        session.add(job)
        await session.commit()
        await session.refresh(job)

    assert job.audio_url is None
    assert job.audio_duration is None
    assert job.title is None
    assert job.song_id is None
    assert job.ingest_attempts == 0


async def test_job_song_id_references_a_song_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        song = Song(
            id="conv-1", title="A Track", source=SOURCE_GENERATED,
            object_key="audio/conv-1.mp3", duration_seconds=42,
        )
        job = Job(
            job_id="job-1", user_id="user-1", prompt="p",
            state=JOB_STATE_READY, task_id="task-1", conversion_id_1="conv-1",
            conversion_id_2="conv-2", song_id="conv-1",
        )
        session.add_all([song, job])
        await session.commit()
        await session.refresh(job)

    assert job.song_id == "conv-1"


# ── Issue #16 columns: client_ip / is_authenticated / failure_handled_at ─────────


async def test_job_client_ip_and_is_authenticated_round_trip_with_defaults(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Design D3: persisted at create time so the watchdog's terminal-failure sweep
    can reconstruct the exact identity+ip that created the job for an accurate
    refund. Defaulted/nullable so every pre-#16 `Job(...)` construction is unaffected."""
    job = Job(
        job_id="job-1", user_id="user-1", prompt="p", state=JOB_STATE_INGEST_PENDING,
        task_id="task-1", conversion_id_1="conv-1", conversion_id_2="conv-2",
    )
    async with sessionmaker() as session:
        session.add(job)
        await session.commit()
        await session.refresh(job)
        assert job.client_ip is None
        assert job.is_authenticated is False

        job.client_ip = "203.0.113.5"
        job.is_authenticated = True
        await session.commit()
        await session.refresh(job)
        assert job.client_ip == "203.0.113.5"
        assert job.is_authenticated is True


async def test_job_failure_handled_at_defaults_to_none_and_round_trips_a_timestamp(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Design D2: the row-claim guard for the terminal-failure refund+notify sweep --
    NULL means "still needs handling"; stamped once refund+notify has run."""
    job = Job(
        job_id="job-1", user_id="user-1", prompt="p", state=JOB_STATE_FAILED,
        task_id="task-1", conversion_id_1="conv-1", conversion_id_2="conv-2",
    )
    async with sessionmaker() as session:
        session.add(job)
        await session.commit()
        await session.refresh(job)
        assert job.failure_handled_at is None

        job.failure_handled_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(job)
        assert job.failure_handled_at is not None
