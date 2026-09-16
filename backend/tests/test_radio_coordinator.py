"""Coordinator logic: initialize-if-absent + version-CAS advance.

Issue #8, criteria #1 (pointer), #2 (advance + anti-repeat + never-empty), #5 (a
listener's song changes at the boundary — this CAS is what moves the pointer the client
re-fetches against). Exercised against in-memory SQLite (`sqlite+aiosqlite`) with a stub
recent-history store; the Postgres advisory lock and real Redis are integration concerns
kept off this fast unit path (see PRD testing decisions).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import pytest

from songforge.models import Base, RadioState, Song
from songforge.radio.coordinator import advance, initialize_if_absent


class _StubHistory:
    """In-memory stand-in for the Redis-backed recent-history store."""

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


async def _add_songs(sessionmaker: async_sessionmaker[AsyncSession], *ids: str) -> None:
    async with sessionmaker() as session:
        for song_id in ids:
            session.add(
                Song(
                    id=song_id,
                    title=song_id,
                    source="static",
                    object_key=f"audio/{song_id}.mp3",
                    duration_seconds=180,
                )
            )
        await session.commit()


async def test_initialize_creates_pointer_at_version_zero_when_songs_exist(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")

    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory())
        assert created is True
        state = await session.get(RadioState, 1)
        assert state is not None
        assert state.version == 0
        assert state.song_id in ("song-1", "song-2")
        assert state.started_at is not None
        assert state.ends_at is not None
        assert state.playback_id is not None


async def test_initialize_is_idle_noop_when_no_songs_exist(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory())
        assert created is False
        assert await session.get(RadioState, 1) is None


async def test_initialize_is_noop_when_pointer_already_exists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1")
    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory()) is True

    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory()) is False


async def test_advance_picks_a_song_not_in_recent_history(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2", "song-3")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version
        current_song_id = before.song_id
        current_playback_id = before.playback_id

    history = _StubHistory(recent={current_song_id} if current_song_id else set())
    async with sessionmaker() as session:
        applied = await advance(session, history, expected_version=current_version)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.version == current_version + 1
        assert after.song_id != current_song_id
        assert after.playback_id != current_playback_id
        assert after.song_id in history.recorded


async def test_advance_never_empty_replays_current_when_only_one_song_exists(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "solo")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    history = _StubHistory(recent={"solo"})
    async with sessionmaker() as session:
        applied = await advance(session, history, expected_version=current_version)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "solo"
        assert after.version == current_version + 1


async def test_advance_with_stale_version_updates_zero_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A stale version-CAS is a no-op: it must NOT double-advance the pointer."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version
        current_song_id = before.song_id

    stale_version = current_version + 999  # guaranteed never to match

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=stale_version)
        assert applied is False
        unchanged = await session.get(RadioState, 1)
        assert unchanged is not None
        assert unchanged.version == current_version
        assert unchanged.song_id == current_song_id
