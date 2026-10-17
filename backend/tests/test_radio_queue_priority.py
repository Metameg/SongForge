"""issue #14, criterion #2: `advance()` pops the user queue first, falling back to
`pick_static` only when it is empty.

Extends `tests/test_radio_coordinator.py`'s exact fixture/stub pattern (same
`_StubHistory`, same in-memory-SQLite `sessionmaker` fixture) with `playback_queue`
seed data. Drives the REAL, unmodified `advance()` -- these tests are RED because
`advance()` today unconditionally calls `pick_static` and never looks at
`playback_queue` at all.

Setup ordering note: every test below establishes the initial pointer via
`initialize_if_absent` BEFORE any generated song is added to the `songs` table. This
is deliberate, not incidental -- `initialize_if_absent`/`advance`'s static-fallback
branch currently queries `select(Song)` with NO `source` filter (a second, related gap
this file's last test names explicitly), so seeding a generated song into the catalog
before the initial pick would make which song `pick_static` lands on non-deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import pytest

from songforge.models import (
    Base,
    PlaybackQueue,
    RadioState,
    Song,
    SOURCE_GENERATED,
    SOURCE_STATIC,
)
from songforge.radio.coordinator import advance, initialize_if_absent


class _StubHistory:
    """Same in-memory recent-history stand-in as `test_radio_coordinator.py`."""

    def __init__(self, recent: set[str] | None = None) -> None:
        self._recent: set[str] = set(recent or ())
        self.recorded: list[str] = []

    async def recent_ids(self) -> set[str]:
        return set(self._recent)

    async def record(self, song_id: str) -> None:
        self.recorded.append(song_id)
        self._recent.add(song_id)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _add_static_songs(
    sessionmaker: async_sessionmaker[AsyncSession], *ids: str
) -> None:
    async with sessionmaker() as session:
        for song_id in ids:
            session.add(
                Song(
                    id=song_id,
                    title=song_id,
                    source=SOURCE_STATIC,
                    object_key=f"audio/{song_id}.mp3",
                    duration_seconds=180,
                )
            )
        await session.commit()


async def _add_generated_song(
    sessionmaker: async_sessionmaker[AsyncSession], song_id: str, *, duration: int = 200
) -> None:
    async with sessionmaker() as session:
        session.add(
            Song(
                id=song_id,
                title=song_id,
                source=SOURCE_GENERATED,
                object_key=f"audio/{song_id}.mp3",
                duration_seconds=duration,
            )
        )
        await session.commit()


async def _enqueue(
    sessionmaker: async_sessionmaker[AsyncSession],
    row_id: int,
    song_id: str,
    *,
    job_id: str | None = None,
) -> None:
    async with sessionmaker() as session:
        session.add(PlaybackQueue(id=row_id, song_id=song_id, job_id=job_id))
        await session.commit()


async def _init_static_pointer(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> int:
    """Establish the initial pointer while the catalog holds ONLY static songs
    (see module docstring), returning its version."""
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        return before.version


async def test_advance_pops_the_user_queue_before_falling_back_to_static(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_static_songs(sessionmaker, "static-1", "static-2")
    current_version = await _init_static_pointer(sessionmaker)

    await _add_generated_song(sessionmaker, "user-song-1")
    await _enqueue(sessionmaker, 1, "user-song-1", job_id="job-1")

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=current_version)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "user-song-1"
        assert after.source == SOURCE_GENERATED
        assert after.version == current_version + 1


async def test_advance_applied_cas_consumes_the_queue_row(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_static_songs(sessionmaker, "static-1")
    current_version = await _init_static_pointer(sessionmaker)

    await _add_generated_song(sessionmaker, "user-song-1")
    await _enqueue(sessionmaker, 1, "user-song-1")

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=current_version)
        assert applied is True

    async with sessionmaker() as session:
        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is not None


async def test_advance_lost_cas_race_leaves_the_queue_row_unconsumed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Mirrors `test_advance_with_stale_version_updates_zero_rows`: a lost race must
    never consume the queue row -- another leader is the one that actually advanced."""
    await _add_static_songs(sessionmaker, "static-1")
    current_version = await _init_static_pointer(sessionmaker)

    await _add_generated_song(sessionmaker, "user-song-1")
    await _enqueue(sessionmaker, 1, "user-song-1")

    stale_version = current_version + 999
    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=stale_version)
        assert applied is False

    async with sessionmaker() as session:
        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None


async def test_advance_from_queue_does_not_record_into_static_recent_history(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Anti-repeat is a static-only concern (criterion #2's design note) -- a
    generated-song advance must never push into the static recent-history store."""
    await _add_static_songs(sessionmaker, "static-1")
    current_version = await _init_static_pointer(sessionmaker)

    await _add_generated_song(sessionmaker, "user-song-1")
    await _enqueue(sessionmaker, 1, "user-song-1")

    history = _StubHistory()
    async with sessionmaker() as session:
        applied = await advance(session, history, expected_version=current_version)
        assert applied is True

    assert history.recorded == []


async def test_advance_pops_the_oldest_queued_song_first_fifo(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_static_songs(sessionmaker, "static-1")
    current_version = await _init_static_pointer(sessionmaker)

    await _add_generated_song(sessionmaker, "user-song-old")
    await _add_generated_song(sessionmaker, "user-song-new")
    await _enqueue(sessionmaker, 1, "user-song-old")
    await _enqueue(sessionmaker, 2, "user-song-new")

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=current_version)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "user-song-old"


async def test_advance_static_fallback_never_selects_a_song_from_the_user_catalog(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Related gap this issue must also close: issue #14 puts static and generated
    songs in the SAME `songs` table (spec #48), so the static-fallback branch's
    candidate query MUST filter to `source == 'static'` -- otherwise a generated song
    sitting in the catalog (already played once, or never queued at all) can leak into
    the plain static rotation outside of ever being explicitly queued and popped.

    Forces `pick_static`'s "avoid recent" branch down to exactly one non-recent
    candidate: with the (buggy, unfiltered) catalog {static-1, user-song-1} and
    static-1 marked recent, the only non-recent candidate is the generated song --
    which a correct, source-filtered query would never offer at all (leaving
    `pick_static` to replay `static-1`, its documented "no non-recent candidate left"
    fallback)."""
    await _add_static_songs(sessionmaker, "static-1")

    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        session.add(
            RadioState(
                id=1,
                song_id="static-1",
                playback_id="pb-0",
                source=SOURCE_STATIC,
                started_at=now,
                ends_at=now,
                version=0,
            )
        )
        await session.commit()

    await _add_generated_song(sessionmaker, "user-song-1")

    history = _StubHistory(recent={"static-1"})
    async with sessionmaker() as session:
        applied = await advance(session, history, expected_version=0)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "static-1"
        assert after.source == SOURCE_STATIC
