"""Probe issue #14's idempotent-enqueue design claim (Review Focus #1): the docstrings
in `radio/queue.enqueue_song` and `jobs/ingest._finalize_ready` state that
`playback_queue` has NO database-level unique constraint on `job_id` -- idempotency is
provided entirely by (a) `_finalize_ready`'s pre-check `SELECT ... WHERE job_id = :id`
before calling `enqueue_song`, backstopped by (b) `claim_next_ingest_job`'s
`FOR UPDATE SKIP LOCKED` claim, which serializes any two attempts to (re-)ingest the
SAME job so the pre-check-then-insert is never itself run concurrently for one job_id.

This file verifies both halves of that claim directly, rather than trusting the
docstring:

1. `radio.queue.enqueue_song` in isolation has NO idempotency of its own -- called
   twice with the same `job_id`, it inserts twice (confirms the "always inserts" claim
   in its own docstring, and confirms *why* the pre-check in `_finalize_ready` is load-
   bearing, not decorative).
2. `_finalize_ready`'s pre-check, when actually exercised sequentially for the same
   `job_id` (the realistic "crash-in-the-gap, re-claimed later" replay -- already
   proven at the `ingest_claimed_job` level in `test_ingest_enqueue.py`), does prevent
   the duplicate.

Together these confirm the idempotency guarantee holds ONLY as long as every caller
goes through the pre-check (i.e. through `_finalize_ready`/`ingest_claimed_job`) under
the job-row lock -- there is no defense-in-depth at the `playback_queue` table itself.
Flagged as a design concern in the phase-4 test report, not fixed here.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, PlaybackQueue, Song, SOURCE_GENERATED
from songforge.radio.queue import enqueue_song

_ids = itertools.count(1)


def _assign_id(mapper: object, connection: object, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_ids)


@pytest.fixture(autouse=True)
def _sqlite_id_shim() -> Iterator[None]:
    event.listen(PlaybackQueue, "before_insert", _assign_id)
    yield
    event.remove(PlaybackQueue, "before_insert", _assign_id)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_enqueue_song_alone_has_no_idempotency_and_will_double_insert(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Documents (does not fix) the design's reliance on the CALLER's pre-check:
    `enqueue_song` is a bare insert with no unique-constraint backstop in the DB
    (`0005_playback_queue.py` creates no `UNIQUE` on `job_id`). Calling it twice for
    the same `job_id` -- as would happen if any future caller skipped
    `_finalize_ready`'s pre-check -- produces two rows, not one. This is the exact
    failure mode Review Focus #1 warns about; it is currently prevented only by
    `_finalize_ready`'s pre-check + `claim_next_ingest_job`'s row lock, not by this
    function or the schema."""
    async with sessionmaker() as session:
        session.add(
            Song(
                id="song-1", title="song-1", source=SOURCE_GENERATED,
                object_key="audio/song-1.mp3", duration_seconds=120,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        await enqueue_song(session, "song-1", "job-1")
        await enqueue_song(session, "song-1", "job-1")
        await session.commit()

    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(PlaybackQueue).where(PlaybackQueue.job_id == "job-1")
            )
        ).all()
        # Documents the current (concerning) behavior: two rows for one job_id when the
        # pre-check is bypassed. See the module docstring -- recommend a
        # `uq_playback_queue_job_id` unique constraint as defense-in-depth.
        assert len(rows) == 2
