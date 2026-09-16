"""``GET /audio/{token}`` — serves fake ``conversion_path`` audio (issue #11 acceptance #5).

Backed by the local royalty-free pool (``SimulatorSettings.audio_pool_dir``). A stale/expired
token (url-expires-before-ingest fault) responds 403 until a fresh one is (re)issued via
``/byId``. Behavior lands in the next TDD phase; this stub only wires the route.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

router = APIRouter()


@router.get("/audio/{token}")
async def get_audio(token: str) -> Response:
    raise HTTPException(
        status_code=501, detail="simulator audio serving not implemented yet"
    )
