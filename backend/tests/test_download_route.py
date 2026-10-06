"""Edge tests for GET /download/{song_id} (issue #39).

The route 302-redirects to a presigned storage URL (browser -> R2 directly; the app never
streams audio bytes) and 404s for an unknown song. Storage is a fake -- no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, Song
from songforge.storage import ObjectStorage
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


async def _seed_song(
    sessionmaker: async_sessionmaker[AsyncSession], title: str = "Neon Rain"
) -> None:
    async with sessionmaker() as session:
        session.add(
            Song(
                id="song-1",
                title=title,
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


def _real_storage():
    from songforge.storage import ObjectStorage

    return ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        public_base_url="https://cdn.example.com/audio-bucket",
    )


async def test_real_storage_location_targets_persisted_object_key(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from urllib.parse import urlparse

    await _seed_song(sessionmaker)
    client = _build_client(sessionmaker, _real_storage())  # type: ignore[arg-type]

    resp = client.get("/download/song-1", follow_redirects=False)

    assert urlparse(resp.headers["location"]).path.endswith("/songforge-audio/audio/song-1.mp3")


async def test_real_storage_location_carries_title_attachment_disposition(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from urllib.parse import parse_qs, urlparse

    await _seed_song(sessionmaker)
    client = _build_client(sessionmaker, _real_storage())  # type: ignore[arg-type]

    resp = client.get("/download/song-1", follow_redirects=False)

    query = parse_qs(urlparse(resp.headers["location"]).query)
    assert query["response-content-disposition"] == ['attachment; filename="Neon Rain.mp3"']


async def test_unknown_song_404_body_leaks_no_metadata(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_song(sessionmaker)
    client = _build_client(sessionmaker, _FakeStorage())

    resp = client.get("/download/nope", follow_redirects=False)

    assert resp.json() == {"detail": "song not found"}


@pytest.mark.parametrize(
    ("title", "expected_filename"),
    [
        ('Say "Hi"', "Say Hi.mp3"),
        ("back\\slash", "backslash.mp3"),
        ("line\nbreak\r\tx\x00", "linebreakx.mp3"),
        ("   padded   ", "padded.mp3"),
        ("\n lead", "lead.mp3"),
        ("", "song-1.mp3"),
        ("   \t\n ", "song-1.mp3"),
        ('""', "song-1.mp3"),
        ("Café 夜の雨 \U0001f3b5", "Café 夜の雨 \U0001f3b5.mp3"),
    ],
)
async def test_title_is_sanitised_into_filename(
    sessionmaker: async_sessionmaker[AsyncSession], title: str, expected_filename: str
) -> None:
    await _seed_song(sessionmaker, title=title)
    storage = _FakeStorage()
    client = _build_client(sessionmaker, storage)

    resp = client.get("/download/song-1", follow_redirects=False)

    assert resp.status_code == 302
    assert storage.calls == [("audio/song-1.mp3", expected_filename)]


@pytest.mark.parametrize(
    "title", ['Say "Hi"', "a\nb\r\nc", "back\\slash", "", "Café 夜の雨", "  x  "]
)
async def test_signed_disposition_is_a_single_line_header_value(
    sessionmaker: async_sessionmaker[AsyncSession], title: str
) -> None:
    await _seed_song(sessionmaker, title=title)
    real = ObjectStorage(
        endpoint_url="http://minio:9000",
        access_key_id="test",
        secret_access_key="test-secret",
        bucket="songforge-audio",
        region="auto",
        public_base_url="https://cdn.example.com/audio-bucket",
    )
    app_client = _build_client(sessionmaker, _FakeStorage())
    app_client.app.dependency_overrides[get_storage_dependency] = lambda: real

    resp = app_client.get("/download/song-1", follow_redirects=False)

    disposition = parse_qs(urlparse(resp.headers["location"]).query)[
        "response-content-disposition"
    ][0]
    assert disposition.startswith('attachment; filename="')
    assert disposition.endswith('.mp3"')
    assert not any(ch in disposition for ch in "\r\n\x00")
    assert disposition.count('"') == 2


async def test_redirect_carries_no_audio_body(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_song(sessionmaker)
    client = _build_client(sessionmaker, _FakeStorage())

    resp = client.get("/download/song-1", follow_redirects=False)

    assert resp.content == b""
    assert resp.headers.get("content-type", "").startswith("audio/") is False
