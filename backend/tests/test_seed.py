"""Static-library seeding is idempotent (acceptance criterion #2 / spec #76).

The row-seed is exercised against in-memory SQLite so it runs with no live datastore;
discovery/title logic is pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import func, select

from songforge.models import Base, Song
from songforge.seed import (
    StaticTrack,
    discover_static_tracks,
    seed_static_library,
)


def _tracks() -> list[StaticTrack]:
    return [
        StaticTrack(id="alpha", title="Alpha", object_key="audio/alpha.mp3", duration_seconds=120),
        StaticTrack(id="beta", title="Beta", object_key="audio/beta.mp3", duration_seconds=None),
    ]


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker:  # type: ignore[type-arg]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_seed_inserts_all_tracks(sessionmaker: async_sessionmaker) -> None:  # type: ignore[type-arg]
    async with sessionmaker() as session:
        inserted = await seed_static_library(session, _tracks())
        assert inserted == 2
        count = (await session.execute(select(func.count()).select_from(Song))).scalar()
        assert count == 2


async def test_seed_is_idempotent(sessionmaker: async_sessionmaker) -> None:  # type: ignore[type-arg]
    async with sessionmaker() as session:
        assert await seed_static_library(session, _tracks()) == 2
    async with sessionmaker() as session:
        # Second run inserts nothing and does not duplicate rows.
        assert await seed_static_library(session, _tracks()) == 0
        count = (await session.execute(select(func.count()).select_from(Song))).scalar()
        assert count == 2


async def test_seed_adds_only_new_tracks(sessionmaker: async_sessionmaker) -> None:  # type: ignore[type-arg]
    async with sessionmaker() as session:
        await seed_static_library(session, _tracks()[:1])
    async with sessionmaker() as session:
        inserted = await seed_static_library(session, _tracks())
        assert inserted == 1


def test_discover_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    assert discover_static_tracks(tmp_path / "nope") == []


def test_discover_scans_mp3s(tmp_path: Path) -> None:
    (tmp_path / "song_one.mp3").write_bytes(b"not-real-audio")
    (tmp_path / "not-audio.txt").write_text("skip me")
    tracks = discover_static_tracks(tmp_path)
    assert [t.id for t in tracks] == ["song_one"]
    assert tracks[0].object_key == "audio/song_one.mp3"
    assert tracks[0].title == "Song One"
