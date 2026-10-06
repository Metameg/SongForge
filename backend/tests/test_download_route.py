"""Edge tests for GET /download/{song_id} (issue #39).

The route 302-redirects to a presigned storage URL (browser -> R2 directly; the app never
streams audio bytes) and 404s for an unknown song. Storage is a fake -- no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, Song
from songforge.web.app import create_app
from songforge.web.routes.download import get_session, get_storage_dependency

SENTINEL_URL = "https://r2.example.com/signed?X-Amz-Expires=300"


class _FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def presigned_download_url(
        self, key: str, filename: str, expires_in: int | None = None
    ) -> str:
        self.calls.append((key, filename))
        return SENTINEL_URL


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _build_client(
    sessionmaker: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> TestClient:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_storage_dependency] = lambda: storage
    return TestClient(app, raise_server_exceptions=False)


async def _seed_song(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        session.add(
            Song(
                id="song-1",
                title="Neon Rain",
                source="generated",
                object_key="audio/song-1.mp3",
            )
        )
        await session.commit()


async def test_known_song_redirects_302_to_presigned_url(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_song(sessionmaker)
    storage = _FakeStorage()
    client = _build_client(sessionmaker, storage)

    resp = client.get("/download/song-1", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == SENTINEL_URL
    assert resp.content == b""


async def test_presign_uses_object_key_and_title_filename(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_song(sessionmaker)
    storage = _FakeStorage()
    client = _build_client(sessionmaker, storage)

    client.get("/download/song-1", follow_redirects=False)

    assert storage.calls == [("audio/song-1.mp3", "Neon Rain.mp3")]


async def test_unknown_song_is_404_and_not_presigned(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    storage = _FakeStorage()
    client = _build_client(sessionmaker, storage)

    resp = client.get("/download/nope", follow_redirects=False)

    assert resp.status_code == 404
    assert storage.calls == []
