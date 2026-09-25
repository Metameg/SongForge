"""Unit tests for the ``playback_queue`` pop/enqueue port (issue #14, criterion #1/#2).

Mirrors `tests/test_radio_history.py`'s seam for `radio.history` and
`tests/test_radio_coordinator.py`'s in-memory-SQLite fixture pattern:
`radio.queue.enqueue_song`/`pop_next_user_song` take the caller's own `AsyncSession`
directly (no Redis/mock port needed -- `playback_queue` lives in the same Postgres
database as `songs`/`jobs`), so these are exercised against `sqlite+aiosqlite`.

RED phase (phase 1): both functions are unimplemented stubs (`raise NotImplementedError`)
in `songforge/radio/queue.py` -- every test below fails on that call, not on import.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, Job, JOB_STATE_READY, PlaybackQueue, Song, SOURCE_GENERATED

# SQLite `create_all` never auto-populates a BigInteger `Identity()` PK (it isn't the
# sqlite rowid alias unless the column literally compiles to `INTEGER` -- `BigInteger`
# compiles to `BIGINT`; see `PlaybackQueue`'s model docstring / `Job.seq`'s matching
# caveat). `enqueue_song` deliberately never sets `id` itself (that's the point -- real
# Postgres generates it), so this unit path needs the same `before_insert` shim as
# `tests/test_create_route.py`.
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


async def _add_song(
    session: AsyncSession, song_id: str, *, source: str = SOURCE_GENERATED
) -> None:
    session.add(
        Song(
            id=song_id,
            title=song_id,
            source=source,
            object_key=f"audio/{song_id}.mp3",
            duration_seconds=181,
        )
    )
    await session.commit()


async def _add_job(session: AsyncSession, job_id: str, *, song_id: str) -> None:
    session.add(
        Job(
            job_id=job_id,
            seq=1,
            user_id="user-1",
            prompt="a song about testing",
            state=JOB_STATE_READY,
            song_id=song_id,
            available_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()


# ── enqueue_song (criterion #1) ────────────────────────────────────────────────────


async def test_enqueue_song_creates_an_unplayed_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.radio.queue import enqueue_song

    async with sessionmaker() as session:
        await _add_song(session, "song-1")
        await _add_job(session, "job-1", song_id="song-1")

    async with sessionmaker() as session:
        await enqueue_song(session, "song-1", "job-1")
        await session.commit()

    async with sessionmaker() as session:
        rows = (await session.scalars(select(PlaybackQueue))).all()
        assert len(rows) == 1
        assert rows[0].song_id == "song-1"
        assert rows[0].job_id == "job-1"
        assert rows[0].played_at is None


async def test_enqueue_song_allows_a_null_job_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`job_id` is nullable (traceability only) -- a queue row must not require one."""
    from songforge.radio.queue import enqueue_song

    async with sessionmaker() as session:
        await _add_song(session, "song-1")

    async with sessionmaker() as session:
        await enqueue_song(session, "song-1", None)
        await session.commit()

    async with sessionmaker() as session:
        row = (await session.scalars(select(PlaybackQueue))).one()
        assert row.job_id is None


# ── pop_next_user_song (criterion #2) ──────────────────────────────────────────────


async def test_pop_next_user_song_returns_none_when_the_queue_is_empty(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.radio.queue import pop_next_user_song

    async with sessionmaker() as session:
        assert await pop_next_user_song(session) is None


async def test_pop_next_user_song_returns_the_oldest_unplayed_row_fifo(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """FIFO by id -- the oldest queued song is popped first, regardless of
    `enqueued_at` (id is the ordering key, per the PlaybackQueue model docstring)."""
    from songforge.radio.queue import pop_next_user_song

    async with sessionmaker() as session:
        await _add_song(session, "song-old")
        await _add_song(session, "song-new")
        session.add(PlaybackQueue(id=1, song_id="song-old", job_id=None))
        session.add(PlaybackQueue(id=2, song_id="song-new", job_id=None))
        await session.commit()

    async with sessionmaker() as session:
        popped = await pop_next_user_song(session)
        assert popped is not None
        assert popped.song_id == "song-old"


async def test_pop_next_user_song_never_returns_an_already_played_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.radio.queue import pop_next_user_song

    async with sessionmaker() as session:
        await _add_song(session, "song-old")
        await _add_song(session, "song-new")
        session.add(
            PlaybackQueue(
                id=1,
                song_id="song-old",
                job_id=None,
                played_at=datetime.now(timezone.utc),
            )
        )
        session.add(PlaybackQueue(id=2, song_id="song-new", job_id=None))
        await session.commit()

    async with sessionmaker() as session:
        popped = await pop_next_user_song(session)
        assert popped is not None
        assert popped.song_id == "song-new"  # the played id=1 row is skipped


async def test_pop_next_user_song_does_not_mark_the_row_played(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`pop_next_user_song` PEEKS -- it must never itself mutate `played_at`. Only an
    APPLIED CAS in `radio.coordinator` may consume the row (see the module docstring)."""
    from songforge.radio.queue import pop_next_user_song

    async with sessionmaker() as session:
        await _add_song(session, "song-1")
        session.add(PlaybackQueue(id=1, song_id="song-1", job_id=None))
        await session.commit()

    async with sessionmaker() as session:
        popped = await pop_next_user_song(session)
        assert popped is not None

    async with sessionmaker() as session:
        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None
