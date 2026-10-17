"""``GET /api/public/v1/byId`` — status + a freshly reissued audio URL (issue #11 #4).

Reports the task's current status (``IN_QUEUE`` before delivery, then ``COMPLETED`` /
``ERROR`` / ``FAILED``) and, once completed, mints a **fresh** (unexpired) audio token every
call — the URL-refresh mechanism the ingest path relies on when the webhook's
``conversion_path`` has expired (``url-expires-before-ingest`` fault, PRD #13). Unknown
``task_id`` -> 404.

Matches the real MusicGPT contract (docs.musicgpt.com): the path is
``/api/public/v1/byId``, both ``task_id`` and ``conversionType`` are required query
params (the value is accepted and ignored here — this fake only generates songs), and
the task fields are NESTED under a ``conversion`` object (see ``schemas.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from songforge.simulator.delivery import build_audio_url
from songforge.simulator.schemas import ByIdConversion, ByIdResponse, ByIdStatus

router = APIRouter()


@router.get("/api/public/v1/byId")
async def by_id(
    task_id: str, conversionType: str, request: Request  # noqa: N803 - real API param name
) -> ByIdResponse:
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
        # `success` is "the lookup succeeded", not the generation outcome -- a found
        # task is always a successful lookup (an unknown task_id already 404'd above);
        # the generation's own ERROR/FAILED/COMPLETED state is `conversion.status`.
        success=True,
        conversion=ByIdConversion(
            task_id=record.task_id,
            conversion_id_1=record.conversion_id_1,
            status=record.status,
            conversion_path_1=audio_url,
            conversion_duration_1=record.duration if completed else None,
            title_1=record.title if completed else None,
        ),
    )
