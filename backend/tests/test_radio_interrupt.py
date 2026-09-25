"""issue #14, criterion #3: a fresh user song interrupts a currently-playing STATIC
song (never a user song), at most once per static song.

Part A tests the PURE decision `should_interrupt` (mirrors `radio.selection.pick_static`
-- no DB/session at all). Part B tests the async, DB-wired `attempt_interrupt`
(mirrors `test_radio_coordinator.py`'s exact sessionmaker/`_StubHistory`/`_FakeRedis`
pattern). Both are RED: neither symbol is implemented yet in
`songforge/radio/coordinator.py` (both raise `NotImplementedError`).
"""

from __future__ import annotations

import json
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
from songforge.radio.coordinator import attempt_interrupt, should_interrupt

# ── Part A: the pure decision ────────────────────────────────────────────────────


def test_should_interrupt_is_true_when_the_current_song_is_static() -> None:
    assert should_interrupt(SOURCE_STATIC) is True


def test_should_interrupt_is_false_when_the_current_song_is_generated() -> None:
    """Never interrupts a user song -- the whole "never" half of criterion #3."""
    assert should_interrupt(SOURCE_GENERATED) is False


# ── Part B: the async DB-wired decision ──────────────────────────────────────────


class _StubHistory:
    def __init__(self, recent: set[str] | None = None) -> None:
        self._recent: set[str] = set(recent or ())
        self.recorded: list[str] = []

    async def recent_ids(self) -> set[str]:
        return set(self._recent)

    async def record(self, song_id: str) -> None:
        self.recorded.append(song_id)
        self._recent.add(song_id)


class _FakeRedis:
    """Same minimal in-memory async Redis double as `test_radio_coordinator.py`."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.set_calls = 0
        self.publish_calls: list[tuple[str, str]] = []

    async def set(self, key: str, value: str) -> None:
        self.set_calls += 1
        self._store[key] = value

    async def publish(self, channel: str, message: str) -> None:
        self.publish_calls.append((channel, message))


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_static_pointer(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    song_id: str = "static-1",
    version: int = 0,
) -> None:
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        session.add(
            Song(
                id=song_id,
                title=song_id,
                source=SOURCE_STATIC,
                object_key=f"audio/{song_id}.mp3",
                duration_seconds=180,
            )
        )
        session.add(
            RadioState(
                id=1,
                song_id=song_id,
                playback_id="pb-static",
                source=SOURCE_STATIC,
                started_at=now,
                ends_at=now,
                version=version,
            )
        )
        await session.commit()


async def _seed_generated_pointer(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    song_id: str = "already-playing-user-song",
    version: int = 1,
) -> None:
    """A pointer already playing a user (generated) song -- e.g. a prior interrupt or
    boundary advance already happened."""
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        session.add(
            Song(
                id=song_id,
                title=song_id,
                source=SOURCE_GENERATED,
                object_key=f"audio/{song_id}.mp3",
                duration_seconds=200,
            )
        )
        session.add(
            RadioState(
                id=1,
                song_id=song_id,
                playback_id="pb-generated",
                source=SOURCE_GENERATED,
                started_at=now,
                ends_at=now,
                version=version,
            )
        )
        await session.commit()


async def _enqueue_generated_song(
    sessionmaker: async_sessionmaker[AsyncSession],
    row_id: int,
    song_id: str,
    *,
    duration: int = 200,
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
        session.add(PlaybackQueue(id=row_id, song_id=song_id, job_id=None))
        await session.commit()


async def test_attempt_interrupt_applies_when_current_is_static_and_queue_nonempty(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_static_pointer(sessionmaker, version=3)
    await _enqueue_generated_song(sessionmaker, 1, "user-song-1")

    history = _StubHistory()
    async with sessionmaker() as session:
        applied = await attempt_interrupt(session, history, expected_version=3)
        assert applied is True

    async with sessionmaker() as session:
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "user-song-1"
        assert after.source == SOURCE_GENERATED
        assert after.version == 4

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is not None  # consumed

    assert history.recorded == []  # never touches static anti-repeat history


async def test_attempt_interrupt_never_fires_when_current_is_already_a_user_song(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """"Never interrupts a user song" + "at most once per static song" end to end:
    the current pointer already reflects a previous interrupt/advance to a generated
    song -- a second `song_ready` wake must be a no-op."""
    await _seed_generated_pointer(sessionmaker, version=1)
    await _enqueue_generated_song(sessionmaker, 1, "another-user-song")

    async with sessionmaker() as session:
        applied = await attempt_interrupt(session, _StubHistory(), expected_version=1)
        assert applied is False

    async with sessionmaker() as session:
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "already-playing-user-song"  # unchanged
        assert after.version == 1

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None  # never consumed


async def test_attempt_interrupt_is_a_noop_when_the_queue_is_empty(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_static_pointer(sessionmaker, version=0)

    async with sessionmaker() as session:
        applied = await attempt_interrupt(session, _StubHistory(), expected_version=0)
        assert applied is False

    async with sessionmaker() as session:
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "static-1"
        assert after.version == 0


async def test_attempt_interrupt_lost_cas_race_leaves_the_queue_row_unconsumed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_static_pointer(sessionmaker, version=0)
    await _enqueue_generated_song(sessionmaker, 1, "user-song-1")

    stale_version = 999
    async with sessionmaker() as session:
        applied = await attempt_interrupt(
            session, _StubHistory(), expected_version=stale_version
        )
        assert applied is False

    async with sessionmaker() as session:
        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None

        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.song_id == "static-1"  # unchanged


async def test_attempt_interrupt_publishes_the_resolved_view_on_an_applied_interrupt(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Mirrors `advance`'s `_write_pointer_best_effort` contract (issue #9/#10) --
    an applied interrupt must set+publish the resolved pointer view exactly like an
    applied boundary advance does, so a connected SSE listener is pushed the change."""
    await _seed_static_pointer(sessionmaker, version=0)
    await _enqueue_generated_song(sessionmaker, 1, "user-song-1")

    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await attempt_interrupt(
            session, _StubHistory(), expected_version=0, redis=redis
        )
        assert applied is True

    assert redis.set_calls == 1
    assert len(redis.publish_calls) == 1
    _channel, payload_raw = redis.publish_calls[0]
    payload = json.loads(payload_raw)
    assert payload["song_id"] == "user-song-1"
    assert payload["version"] == 1


async def test_attempt_interrupt_does_not_publish_on_a_lost_cas_race(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_static_pointer(sessionmaker, version=0)
    await _enqueue_generated_song(sessionmaker, 1, "user-song-1")

    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await attempt_interrupt(
            session, _StubHistory(), expected_version=999, redis=redis
        )
        assert applied is False

    assert redis.set_calls == 0
    assert redis.publish_calls == []
