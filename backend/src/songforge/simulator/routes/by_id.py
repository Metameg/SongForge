"""``GET /byId`` — status + a freshly reissued audio URL (issue #11 acceptance #4).

Reports the task's current status (``IN_QUEUE`` before delivery, then ``COMPLETED`` /
``ERROR`` / ``FAILED``) and, once completed, mints a **fresh** (unexpired) audio token every
call — the URL-refresh mechanism the ingest path relies on when the webhook's
``conversion_path`` has expired (``url-expires-before-ingest`` fault, PRD #13). Unknown
``task_id`` -> 404. Field names are a documented, internally-consistent placeholder (see
``schemas.py``), reconciled with the real client when the ingest issue lands.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from songforge.simulator.delivery import build_audio_url
from songforge.simulator.schemas import ByIdResponse, ByIdStatus

router = APIRouter()


@router.get("/byId")
async def by_id(task_id: str, request: Request) -> ByIdResponse:
    store = request.app.state.store
    settings = request.app.state.settings

    record = store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown task_id")

    completed = record.status is ByIdStatus.COMPLETED
    audio_url = None
    if completed:
        # Refresh: hand back a brand-new, unexpired URL for the same generation.
        audio_url = build_audio_url(settings, store.mint_token(task_id, expired=False))

    return ByIdResponse(
        task_id=record.task_id,
        status=record.status,
        conversion_id_1=record.conversion_id_1,
        conversion_id_2=record.conversion_id_2,
        audio_url=audio_url,
        conversion_duration=record.duration if completed else None,
        title=record.title if completed else None,
    )
