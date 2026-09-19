"""Coordinator logic: initialize-if-absent + version-CAS advance.

Issue #8, criteria #1 (pointer), #2 (advance + anti-repeat + never-empty), #5 (a
listener's song changes at the boundary — this CAS is what moves the pointer the client
re-fetches against). Exercised against in-memory SQLite (`sqlite+aiosqlite`) with a stub
recent-history store; the Postgres advisory lock and real Redis are integration concerns
kept off this fast unit path (see PRD testing decisions).
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import pytest

from songforge.metrics import REGISTRY
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


async def test_advance_with_stale_version_does_not_record_history(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A lost CAS race must not push anything into recent-history — nothing advanced,
    so nothing was "just played"."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    stale_version = current_version + 999
    history = _StubHistory()
    async with sessionmaker() as session:
        applied = await advance(session, history, expected_version=stale_version)
        assert applied is False
        assert history.recorded == []


async def test_advance_with_no_songs_in_catalog_is_a_safe_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """``advance`` on an empty catalog (no songs at all) returns False and never
    raises — the idle case, same as ``initialize_if_absent``."""
    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=0)
        assert applied is False
        assert await session.get(RadioState, 1) is None


async def test_advance_moves_the_window_forward_by_the_song_duration(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A successful CAS mints a fresh window: it starts no earlier than the old one and
    spans exactly the chosen song's duration (criterion #5 — the client re-anchors to
    this new window)."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version
        before_started_at = before.started_at
        assert before_started_at is not None

    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=current_version)
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.started_at is not None
        assert after.ends_at is not None
        assert after.started_at >= before_started_at
        assert (after.ends_at - after.started_at).total_seconds() == 180


async def test_initialize_uses_default_track_seconds_when_song_has_no_duration(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A song with no recorded ``duration_seconds`` falls back to the configured
    ``radio_default_track_seconds`` window, not a zero-length or unbounded one."""
    async with sessionmaker() as session:
        session.add(
            Song(
                id="undated",
                title="Undated",
                source="static",
                object_key="audio/undated.mp3",
                duration_seconds=None,
            )
        )
        await session.commit()

    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory())
        assert created is True
        state = await session.get(RadioState, 1)
        assert state is not None
        assert state.started_at is not None
        assert state.ends_at is not None
        assert (state.ends_at - state.started_at).total_seconds() == 180


async def test_advance_increments_the_radio_advances_metric_on_success(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The ``songforge_radio_advances_total`` counter only counts *applied* CAS
    advances, never lost-race attempts."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    before_count = REGISTRY.get_sample_value("songforge_radio_advances_total") or 0.0
    async with sessionmaker() as session:
        applied = await advance(session, _StubHistory(), expected_version=current_version)
        assert applied is True
    after_count = REGISTRY.get_sample_value("songforge_radio_advances_total") or 0.0
    assert after_count == before_count + 1


# ── Coordinator writes the Redis pointer key (issue #9, design D2) ──────────
#
# Contract (not yet implemented -- RED phase): `initialize_if_absent` and `advance`
# gain an optional `redis: Redis | None = None` keyword parameter (backward-compatible
# with every test above, which omits it). When a `redis` is given, a SUCCESSFUL
# init/CAS best-effort writes the resolved view to the configured pointer key -- D5's
# stated default "radio:pointer" -- as a JSON string; a lost CAS race or a no-op
# initialize must NOT write. A Redis failure must be caught and logged, never raise
# out of `advance`/`initialize_if_absent` (Postgres remains the source of truth).

RADIO_POINTER_REDIS_KEY = "radio:pointer"


class _FakeRedis:
    """Minimal in-memory async Redis double. The coordinator is a pointer WRITER
    (design D2), so only `set` is needed here -- read-path fakes live in
    `test_pointer_cache.py` / `test_now_playing.py`. Issue #10 adds `publish`: the
    coordinator also fans the resolved view out to Redis pub/sub so `/events` (the SSE
    endpoint) can relay it without polling -- `publish_calls` captures each
    `(channel, message)` pair the coordinator sends."""

    def __init__(
        self,
        *,
        raise_on_set: Exception | None = None,
        raise_on_publish: Exception | None = None,
    ) -> None:
        self._store: dict[str, str] = {}
        self._raise_on_set = raise_on_set
        self._raise_on_publish = raise_on_publish
        self.set_calls = 0
        self.publish_calls: list[tuple[str, str]] = []

    async def set(self, key: str, value: str) -> None:
        self.set_calls += 1
        if self._raise_on_set is not None:
            raise self._raise_on_set
        self._store[key] = value

    async def publish(self, channel: str, message: str) -> None:
        if self._raise_on_publish is not None:
            raise self._raise_on_publish
        self.publish_calls.append((channel, message))


async def test_initialize_writes_the_resolved_view_to_the_redis_pointer_key(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    redis = _FakeRedis()

    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory(), redis=redis)
        assert created is True
        state = await session.get(RadioState, 1)
        assert state is not None

    assert RADIO_POINTER_REDIS_KEY in redis._store
    payload = json.loads(redis._store[RADIO_POINTER_REDIS_KEY])
    assert payload["song_id"] == state.song_id
    assert payload["version"] == 0
    assert payload["playback_id"] == state.playback_id


async def test_initialize_noop_does_not_write_to_redis(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A pointer that already exists -> `initialize_if_absent` is a no-op and must
    not touch Redis (nothing changed to publish)."""
    await _add_songs(sessionmaker, "song-1")
    redis = _FakeRedis()
    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory(), redis=redis) is True
    redis.set_calls = 0

    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory(), redis=redis) is False
    assert redis.set_calls == 0


async def test_initialize_redis_write_failure_does_not_fail_initialization(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Best-effort (design D2): a Redis error must not prevent (or raise out of) a
    successful initialize."""
    await _add_songs(sessionmaker, "song-1")
    redis = _FakeRedis(raise_on_set=ConnectionError("redis down"))

    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory(), redis=redis)
        assert created is True
        assert await session.get(RadioState, 1) is not None


async def test_advance_writes_the_updated_view_to_redis_on_a_successful_cas(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=current_version, redis=redis
        )
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None

    payload = json.loads(redis._store[RADIO_POINTER_REDIS_KEY])
    assert payload["song_id"] == after.song_id
    assert payload["version"] == current_version + 1
    assert payload["playback_id"] == after.playback_id


async def test_advance_does_not_write_to_redis_on_a_lost_cas_race(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    stale_version = current_version + 999
    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=stale_version, redis=redis
        )
        assert applied is False

    assert redis.set_calls == 0


async def test_advance_redis_write_failure_does_not_fail_the_advance(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Best-effort (design D2): a Redis error on the write-back must not fail (or
    raise out of) a successful CAS advance -- Postgres remains authoritative."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version
        current_song_id = before.song_id

    redis = _FakeRedis(raise_on_set=ConnectionError("redis down"))
    async with sessionmaker() as session:
        # Mark the current song as recently played so `pick_static` deterministically
        # advances to the *other* song (matching the `_StubHistory(recent=...)` pattern
        # used elsewhere in this file); otherwise the two-song `random.choice` makes the
        # `after.song_id != current_song_id` assertion a coin flip.
        applied = await advance(
            session,
            _StubHistory(recent={current_song_id} if current_song_id else set()),
            expected_version=current_version,
            redis=redis,
        )
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.version == current_version + 1
        assert after.song_id != current_song_id


# ── Coordinator publishes to Redis pub/sub on advance (issue #10, criterion #2) ─────
#
# Contract (not yet implemented -- RED phase): after the existing best-effort
# `redis.set` (design D2), a GENUINE init/applied-CAS also best-effort `redis.publish`es
# the same resolved view (JSON) to the pub/sub channel `/events` (the SSE endpoint,
# issue #10) subscribes to -- the context pack's stated default channel name
# "radio:pointer:changed" -- so a connected listener is pushed the change instead of
# waiting on a poll. Exactly mirrors the existing `set` contract: a lost CAS race or a
# no-op initialize must NOT publish (nothing changed to announce), and a publish
# failure must be caught and logged, never raise out of `advance`/`initialize_if_absent`
# (Postgres has already committed by that point).

RADIO_POINTER_CHANNEL = "radio:pointer:changed"


async def test_initialize_publishes_the_resolved_view_on_a_genuine_initialize(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    redis = _FakeRedis()

    async with sessionmaker() as session:
        created = await initialize_if_absent(session, _StubHistory(), redis=redis)
        assert created is True
        state = await session.get(RadioState, 1)
        assert state is not None

    assert len(redis.publish_calls) == 1
    channel, payload_raw = redis.publish_calls[0]
    assert channel == RADIO_POINTER_CHANNEL
    payload = json.loads(payload_raw)
    assert payload["song_id"] == state.song_id
    assert payload["version"] == 0
    assert payload["playback_id"] == state.playback_id


async def test_initialize_noop_does_not_publish(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A pointer that already exists -> `initialize_if_absent` is a no-op and must
    not publish (nothing changed to announce)."""
    await _add_songs(sessionmaker, "song-1")
    redis = _FakeRedis()
    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory(), redis=redis) is True
    redis.publish_calls.clear()

    async with sessionmaker() as session:
        assert await initialize_if_absent(session, _StubHistory(), redis=redis) is False
    assert redis.publish_calls == []


async def test_advance_publishes_the_updated_view_on_a_successful_cas(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=current_version, redis=redis
        )
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None

    assert len(redis.publish_calls) == 1
    channel, payload_raw = redis.publish_calls[0]
    assert channel == RADIO_POINTER_CHANNEL
    payload = json.loads(payload_raw)
    assert payload["song_id"] == after.song_id
    assert payload["version"] == current_version + 1
    assert payload["playback_id"] == after.playback_id


async def test_advance_does_not_publish_on_a_lost_cas_race(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version

    stale_version = current_version + 999
    redis = _FakeRedis()
    async with sessionmaker() as session:
        applied = await advance(
            session, _StubHistory(), expected_version=stale_version, redis=redis
        )
        assert applied is False

    assert redis.publish_calls == []


async def test_advance_publish_failure_does_not_fail_the_advance(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Best-effort (mirrors the existing `set`-failure contract): a publish error must
    not prevent (or raise out of) a successful CAS advance -- Postgres remains
    authoritative and the advance must still be reported applied."""
    await _add_songs(sessionmaker, "song-1", "song-2")
    async with sessionmaker() as session:
        await initialize_if_absent(session, _StubHistory())
        before = await session.get(RadioState, 1)
        assert before is not None
        current_version = before.version
        current_song_id = before.song_id

    redis = _FakeRedis(raise_on_publish=ConnectionError("redis down"))
    async with sessionmaker() as session:
        applied = await advance(
            session,
            _StubHistory(recent={current_song_id} if current_song_id else set()),
            expected_version=current_version,
            redis=redis,
        )
        assert applied is True
        after = await session.get(RadioState, 1)
        assert after is not None
        assert after.version == current_version + 1
        assert after.song_id != current_song_id
