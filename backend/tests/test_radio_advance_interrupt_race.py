"""Edge/error-path coverage for issue #14 that the phase-3 suites didn't pin down:

1. **Boundary-vs-interrupt race funnels through ONE version-CAS.** `worker/radio_coordinator.py`
   races a `song_ready` wake (-> `attempt_interrupt`) against the clock-driven boundary
   sleep (-> `advance`), both reading the SAME prior pointer version and popping the SAME
   queue row. `test_radio_interrupt.py`/`test_radio_queue_priority.py` each prove a single
   lost-CAS no-ops in isolation; this file additionally proves the two *different* entry
   points, given the identical starting state, cannot BOTH apply -- exactly one advance
   happens, the queue row is consumed exactly once, and the second call is a clean no-op
   (never a double-advance, never a second consumption, never an exception).

2. **A queue row whose `song_id` doesn't resolve to a `Song` is a safe no-op**, not a
   coordinator crash -- `_advance_to_user_song`'s defensive `if song is None` branch
   (`radio/coordinator.py`) has no existing test; this covers it via both entry points
   (`advance` and `attempt_interrupt`).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import (
    Base,
    PlaybackQueue,
    RadioState,
    Song,
    SOURCE_GENERATED,
    SOURCE_STATIC,
)
from songforge.radio.coordinator import advance, attempt_interrupt, initialize_if_absent


class _StubHistory:
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


async def _add_static_song(
    sessionmaker: async_sessionmaker[AsyncSession], song_id: str
) -> None:
    async with sessionmaker() as session:
        session.add(
            Song(
                id=song_id, title=song_id, source=SOURCE_STATIC,
                object_key=f"audio/{song_id}.mp3", duration_seconds=180,
            )
        )
        await session.commit()


async def _add_generated_song(
    sessionmaker: async_sessionmaker[AsyncSession], song_id: str
) -> None:
    async with sessionmaker() as session:
        session.add(
            Song(
                id=song_id, title=song_id, source=SOURCE_GENERATED,
                object_key=f"audio/{song_id}.mp3", duration_seconds=120,
            )
        )
        await session.commit()


async def test_wake_interrupt_and_boundary_advance_share_one_cas_no_double_advance(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates the exact race `worker/radio_coordinator.py` guards against: a
    `song_ready` wake and the boundary timer both fire off the SAME prior pointer read
    (`source='static'`, `version=V`) with exactly one queued user song. Whichever call
    reaches the CAS first (here: the wake's `attempt_interrupt`) applies it and consumes
    the row; the other (here: the boundary's `advance`, still holding the stale
    `expected_version=V`) must see its own CAS lose (0 rows) -- and, because the queue is
    now empty, must fall through to the static path and ALSO lose there (same stale
    version) rather than silently reusing/duplicating anything."""
    await _add_static_song(sessionmaker, "static-1")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        assert before.source == SOURCE_STATIC
        racing_version = before.version

    await _add_generated_song(sessionmaker, "user-song-1")
    async with sessionmaker() as session:
        session.add(PlaybackQueue(id=1, song_id="user-song-1", job_id="job-1"))
        await session.commit()

    # The wake path wins the race first.
    async with sessionmaker() as session:
        interrupted = await attempt_interrupt(
            session, _StubHistory(), expected_version=racing_version
        )
        assert interrupted is True

    async with sessionmaker() as session:
        after_interrupt = await session.get(RadioState, 1)
        assert after_interrupt is not None
        assert after_interrupt.song_id == "user-song-1"
        assert after_interrupt.source == SOURCE_GENERATED
        assert after_interrupt.version == racing_version + 1

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is not None  # consumed exactly once

    # The boundary path, still holding the now-stale `racing_version`, loses its own
    # CAS -- it must NOT re-consume the (already-consumed) row, double-advance the
    # pointer, or raise.
    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=racing_version
        )
        assert applied is False

    async with sessionmaker() as session:
        final = await session.get(RadioState, 1)
        assert final is not None
        # Untouched by the loser -- still exactly what the winning interrupt set.
        assert final.song_id == "user-song-1"
        assert final.version == racing_version + 1

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is not None  # still consumed exactly once, not twice


async def test_boundary_advance_wins_then_wake_interrupt_is_a_clean_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Same race, opposite winner: the boundary's `advance` applies first. The pointer
    is now `source='generated'`, so the wake's `attempt_interrupt` -- reading the
    CURRENT pointer, not a stale copy -- finds `should_interrupt` false and no-ops
    without even attempting a CAS (the "never interrupts a user song" guard doing its
    job under a genuine race, not just the isolated unit test)."""
    await _add_static_song(sessionmaker, "static-1")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        racing_version = before.version

    await _add_generated_song(sessionmaker, "user-song-1")
    async with sessionmaker() as session:
        session.add(PlaybackQueue(id=1, song_id="user-song-1", job_id="job-1"))
        await session.commit()

    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=racing_version
        )
        assert applied is True

    async with sessionmaker() as session:
        interrupted = await attempt_interrupt(
            session, _StubHistory(), expected_version=racing_version
        )
        assert interrupted is False

    async with sessionmaker() as session:
        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is not None  # consumed once by the winning `advance` only


async def test_advance_with_a_dangling_queue_row_is_a_safe_noop_not_a_crash(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Defensive branch coverage: `_advance_to_user_song` guards against a
    `playback_queue` row whose `song_id` doesn't resolve to a `Song` (the FK guarantees
    this can't happen on real Postgres, but nothing should crash the coordinator loop if
    it somehow does -- e.g. a hand-rolled fixture, a future migration bug, or SQLite's
    lack of FK enforcement in production-adjacent tooling). `advance()` must return
    `False` and leave the dangling row unconsumed rather than raising."""
    await _add_static_song(sessionmaker, "static-1")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        version = before.version

    async with sessionmaker() as session:
        # No matching `Song` row for "ghost-song" -- SQLite doesn't enforce the FK here.
        session.add(PlaybackQueue(id=1, song_id="ghost-song", job_id="job-1"))
        await session.commit()

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=version)
        assert applied is False

    async with sessionmaker() as session:
        pointer = await session.get(RadioState, 1)
        assert pointer is not None
        assert pointer.song_id == "static-1"  # untouched
        assert pointer.version == version

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None  # left for a human to clean up, not consumed


async def test_attempt_interrupt_with_a_dangling_queue_row_is_a_safe_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Same defensive-branch coverage via the `attempt_interrupt` entry point."""
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        session.add(
            Song(
                id="static-1", title="static-1", source=SOURCE_STATIC,
                object_key="audio/static-1.mp3", duration_seconds=180,
            )
        )
        session.add(
            RadioState(
                id=1, song_id="static-1", playback_id="pb-static",
                source=SOURCE_STATIC, started_at=now, ends_at=now, version=0,
            )
        )
        session.add(PlaybackQueue(id=1, song_id="ghost-song", job_id="job-1"))
        await session.commit()

    async with sessionmaker() as session:
        applied = await attempt_interrupt(session, _StubHistory(), expected_version=0)
        assert applied is False

    async with sessionmaker() as session:
        pointer = await session.get(RadioState, 1)
        assert pointer is not None
        assert pointer.song_id == "static-1"  # unchanged

        row = await session.get(PlaybackQueue, 1)
        assert row is not None
        assert row.played_at is None
