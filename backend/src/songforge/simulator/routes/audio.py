"""``GET /audio/{token}`` — serves fake ``conversion_path`` audio (issue #11 acceptance #5).

Bytes come from the local royalty-free pool (``SimulatorSettings.audio_pool_dir``) so a real
ingest consumer can download → MinIO; when the pool is absent (unit tests), a tiny synthetic
payload stands in. An expired/unknown token (the ``url-expires-before-ingest`` fault, before a
``/byId`` refresh) responds ``403`` instead of bytes.
"""

from __future__ import annotations

import functools
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()

_AUDIO_MEDIA_TYPE = "audio/mpeg"
# Minimal non-empty MP3-ish payload for when no local pool file is available (tests).
_SYNTHETIC_AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 256


@functools.lru_cache(maxsize=8)
def load_sample_audio(audio_pool_dir: str) -> bytes:
    """First ``*.mp3`` in the pool, or a synthetic fallback. Cached per pool dir."""
    pool = Path(audio_pool_dir)
    if pool.is_dir():
        for candidate in sorted(pool.glob("*.mp3")):
            try:
                return candidate.read_bytes()
            except OSError:
                continue
    return _SYNTHETIC_AUDIO


@router.get("/audio/{token}")
async def get_audio(token: str, request: Request) -> Response:
    store = request.app.state.store
    if not store.has_token(token):
        raise HTTPException(status_code=404, detail="unknown audio token")
    if store.is_expired(token):
        raise HTTPException(status_code=403, detail="audio url expired")
    audio = load_sample_audio(request.app.state.settings.audio_pool_dir)
    return Response(content=audio, media_type=_AUDIO_MEDIA_TYPE)
