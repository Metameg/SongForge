"""``GET /download/{song_id}`` -- 302 to a presigned R2 download URL (issue #39)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.db import get_sessionmaker
from songforge.models import Song
from songforge.storage import ObjectStorage, get_storage

_UNSAFE_FILENAME_CHARS = re.compile(r'["\\\x00-\x1f\x7f]')

router = APIRouter(tags=["download"])


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


def get_storage_dependency() -> ObjectStorage:
    """Injectable storage; overridden in tests."""
    return get_storage()


@router.get("/download/{song_id}")
async def download(
    song_id: str,
    session: AsyncSession = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage_dependency),
) -> RedirectResponse:
    song = await session.get(Song, song_id)
    if song is None:
        raise HTTPException(status_code=404, detail="song not found")
    # Keep the Content-Disposition header well-formed: drop quotes, backslashes, controls.
    title = _UNSAFE_FILENAME_CHARS.sub("", song.title).strip() or song.id
    url = storage.presigned_download_url(song.object_key, f"{title}.mp3")
    return RedirectResponse(url, status_code=302)
