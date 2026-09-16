"""Edge tests for GET /now-playing (issue #8, criteria #1, #3).

Drives the FastAPI app through real HTTP (`TestClient`) with an in-memory SQLite session
override and a seeded `radio_state` pointer — the system edge, per this repo's testing
conventions (see test_web_app.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, RadioState, Song
from songforge.web.app import create_app
from songforge.web.routes.now_playing import get_session

EXPECTED_BODY_KEYS = {
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
