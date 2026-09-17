"""Edge tests for GET /now-playing (issue #8, criteria #1, #3).

Drives the FastAPI app through real HTTP (`TestClient`) with an in-memory SQLite session
override and a seeded `radio_state` pointer — the system edge, per this repo's testing
conventions (see test_web_app.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, RadioState, Song
from songforge.web.app import create_app
from songforge.web.routes.now_playing import get_session

# NOTE: `songforge.radio.pointer_cache` and the `get_redis_dependency` /
# `get_pg_loader_dependency` overrides on `now_playing` do not exist yet (issue #9,
# RED phase). Importing them at module scope would fail collection of this ENTIRE
# file, including the pre-existing passing tests above. Those two new dependency
# symbols and `PointerRecord` are therefore imported locally, inside only the new
# pointer-cache section below, so the baseline tests above stay green and collectible
# while the new tests fail explicitly (ImportError) when they run.

EXPECTED_BODY_KEYS = {
    "status",
    "song_id",
    "title",
    "source",
    "object_key",
    "audio_url",
    "started_at",
    "ends_at",
    "duration",
    "playback_id",
    "version",
    "server_time",
}


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture()
def app_client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> TestClient:
    """A TestClient whose DB session dependency reads/writes the seeded in-memory DB."""

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


async def _seed_pointer(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    started_delta_seconds: float,
    duration_seconds: int = 180,
    version: int = 3,
) -> tuple[datetime, datetime]:
    started_at = datetime.now(timezone.utc) + timedelta(seconds=started_delta_seconds)
    ends_at = started_at + timedelta(seconds=duration_seconds)
    async with sessionmaker() as session:
        session.add(
            Song(
                id="song-1",
                title="Song One",
                source="static",
                object_key="audio/song-1.mp3",
                duration_seconds=duration_seconds,
            )
        )
        session.add(
            RadioState(
                id=1,
                song_id="song-1",
                playback_id="pb-1",
                source="static",
                started_at=started_at,
                ends_at=ends_at,
                version=version,
            )
        )
        await session.commit()
    return started_at, ends_at


async def test_now_playing_returns_full_pointer_shape(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Body includes the pointer fields plus `server_time` (criterion #3)."""
    started_at, ends_at = await _seed_pointer(sessionmaker, started_delta_seconds=-30)

    resp = app_client.get("/now-playing")

    assert resp.status_code == 200
    body = resp.json()
    assert EXPECTED_BODY_KEYS.issubset(body.keys())
    # The playing path must carry a "playing" discriminator, symmetric with the idle
    # body's {"status": "idle"} — the frontend's discriminated union and Player.tsx
    # branch on status === "playing" (issue #8, criteria #4/#5).
    assert body["status"] == "playing"
    assert body["song_id"] == "song-1"
    assert body["title"] == "Song One"
    assert body["source"] == "static"
    assert body["object_key"] == "audio/song-1.mp3"
    assert body["playback_id"] == "pb-1"
    assert body["version"] == 3
    assert body["duration"] == 180


async def test_now_playing_includes_server_time_for_clock_skew_correction(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = datetime.now(timezone.utc)
    await _seed_pointer(sessionmaker, started_delta_seconds=-10)

    resp = app_client.get("/now-playing")
    after = datetime.now(timezone.utc)

    body = resp.json()
    server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
    assert before <= server_time <= after


async def test_now_playing_timestamps_are_tz_aware_utc(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`started_at`/`ends_at`/`server_time` must carry a UTC offset in the wire format.

    The frontend (`frontend/lib/sync.ts`) parses these with JS `Date.parse`, which
    treats an offset-less ISO string as *local time*, not UTC — silently corrupting the
    playback-sync math by the client's timezone offset. `started_at`/`ends_at` are
    written tz-aware (`datetime.now(timezone.utc)`) but round-trip through this test's
    SQLite session, which (unlike Postgres/asyncpg) drops `tzinfo` on read-back; this
    pins that the response still carries an explicit UTC offset regardless.
    """
    await _seed_pointer(sessionmaker, started_delta_seconds=-30)

    resp = app_client.get("/now-playing")

    body = resp.json()
    for field in ("started_at", "ends_at", "server_time"):
        raw = body[field]
        assert raw.endswith("+00:00") or raw.endswith("Z"), (
            f"{field}={raw!r} has no UTC offset — a bare Date.parse() on the frontend "
            "would interpret it as local time, not UTC"
        )
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


async def test_now_playing_idle_when_no_pointer_exists(app_client: TestClient) -> None:
    """No `radio_state` row yet (never initialized) -> a clear not-playing signal."""
    resp = app_client.get("/now-playing")

    assert resp.status_code == 503
    assert resp.json() == {"status": "idle"}


async def test_now_playing_idle_when_pointer_song_id_does_not_resolve(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A pointer referencing a song id no longer in the catalog is idle, not a 500."""
    started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    async with sessionmaker() as session:
        session.add(
            RadioState(
                id=1,
                song_id="missing-song",
                playback_id="pb-1",
                source="static",
                started_at=started_at,
                ends_at=started_at + timedelta(seconds=180),
                version=1,
            )
        )
        await session.commit()

    resp = app_client.get("/now-playing")

    assert resp.status_code == 503
    assert resp.json() == {"status": "idle"}


async def test_now_playing_idle_when_pointer_has_no_song_id(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A pointer row exists (e.g. inserted but never populated by the coordinator) with
    every field still unset -> idle, not a crash on the None fields."""
    async with sessionmaker() as session:
        session.add(RadioState(id=1, version=0))
        await session.commit()

    resp = app_client.get("/now-playing")

    assert resp.status_code == 503
    assert resp.json() == {"status": "idle"}


async def test_now_playing_server_time_does_not_go_backwards_between_calls(
    app_client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`server_time` reflects the server's live clock, not a cached/static value."""
    await _seed_pointer(sessionmaker, started_delta_seconds=-10)

    first = datetime.fromisoformat(
        app_client.get("/now-playing").json()["server_time"].replace("Z", "+00:00")
    )
    second = datetime.fromisoformat(
        app_client.get("/now-playing").json()["server_time"].replace("Z", "+00:00")
    )

    assert second >= first


# ── Pointer-cache-backed /now-playing (issue #9, criteria #2 + #4) ──────────
#
# The route now serves from a process-local `PointerCache` (design D3, held on
# `app.state.pointer_cache` so each `create_app()` instance — and so each test — gets
# its own, avoiding cross-test pollution of a would-be process-global cache) warmed
# from Redis (design D4), falling back to Postgres only when Redis is down/empty.
# These tests inject a fake Redis via the new `get_redis_dependency` override and a
# spy Postgres loader via the new `get_pg_loader_dependency` override (both on
# `songforge.web.routes.now_playing`) so they can assert the criterion-#4 invariant:
# with Redis populated, repeated `/now-playing` reads hit Postgres ZERO times.

# D5's stated default key name; not read from config here since config.py does not
# yet declare `radio_pointer_redis_key` (Phase 2/3 wiring) -- these tests seed the
# fake Redis directly under the name the route will look up by default.
RADIO_POINTER_REDIS_KEY = "radio:pointer"


class _FakeRedis:
    """Same shape as the double in `test_pointer_cache.py`, kept file-local per this
    repo's per-file stub convention (see `_StubHistory` in `test_radio_coordinator.py`)."""

    def __init__(self, *, raise_on_get: Exception | None = None) -> None:
        self._store: dict[str, str] = {}
        self._raise_on_get = raise_on_get
        self.get_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value


def _make_spy_pg_loader() -> tuple[Any, dict[str, int]]:
    """Wraps the REAL `get_now_playing` PG loader so the fallback path still reads
    genuine data from the seeded SQLite session, while counting invocations."""
    from songforge.radio.state import get_now_playing as real_get_now_playing

    calls = {"count": 0}

    async def _spy(session: AsyncSession) -> Any:
        calls["count"] += 1
        return await real_get_now_playing(session)

    return _spy, calls


def _build_pointer_app_client(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: _FakeRedis,
    pg_loader: Any,
) -> TestClient:
    # Local import: see the NOTE at the top of this file -- these two dependencies
    # don't exist yet (RED phase for issue #9).
    from songforge.web.routes.now_playing import (
        get_pg_loader_dependency,
        get_redis_dependency,
    )

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis_dependency] = lambda: redis
    app.dependency_overrides[get_pg_loader_dependency] = lambda: pg_loader
    return TestClient(app)


async def test_now_playing_served_from_redis_never_touches_postgres(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Redis holds the resolved view -> served straight from it. Postgres is seeded
    with a DIFFERENT song so a passing response proves it truly came from Redis, not
    a PG read that happened to agree (criteria #2 + #4)."""
    from songforge.radio.pointer_cache import PointerRecord  # local: see NOTE above

    await _seed_pointer(sessionmaker, started_delta_seconds=-30)  # seeds "song-1"
    started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    redis_record = PointerRecord(
        song_id="redis-song",
        title="From Redis",
        source="static",
        object_key="audio/redis-song.mp3",
        audio_url="http://cdn.test/audio/redis-song.mp3",
        started_at=started_at.isoformat(),
        ends_at=(started_at + timedelta(seconds=180)).isoformat(),
        duration_seconds=180,
        playback_id="pb-redis",
        version=9,
    )
    redis = _FakeRedis()
    redis._store[RADIO_POINTER_REDIS_KEY] = redis_record.to_json()
    pg_loader, calls = _make_spy_pg_loader()
    client = _build_pointer_app_client(sessionmaker, redis, pg_loader)

    for _ in range(3):
        resp = client.get("/now-playing")
        assert resp.status_code == 200
        assert resp.json()["song_id"] == "redis-song"

    assert calls["count"] == 0


async def test_now_playing_falls_back_to_postgres_when_redis_is_down(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """D4 step 3: Redis erroring must still return the correct (Postgres-sourced)
    body, not a 500."""
    await _seed_pointer(sessionmaker, started_delta_seconds=-30)  # seeds "song-1"
    redis = _FakeRedis(raise_on_get=ConnectionError("redis down"))
    pg_loader, calls = _make_spy_pg_loader()
    client = _build_pointer_app_client(sessionmaker, redis, pg_loader)

    resp = client.get("/now-playing")

    assert resp.status_code == 200
    body = resp.json()
    assert body["song_id"] == "song-1"
    assert calls["count"] == 1


async def test_now_playing_idle_503_unchanged_when_redis_down_and_no_pg_pointer(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The idle contract (issue #8) must be unaffected by the new cache layer."""
    redis = _FakeRedis(raise_on_get=ConnectionError("redis down"))
    pg_loader, calls = _make_spy_pg_loader()
    client = _build_pointer_app_client(sessionmaker, redis, pg_loader)

    resp = client.get("/now-playing")

    assert resp.status_code == 503
    assert resp.json() == {"status": "idle"}
    assert calls["count"] == 1
