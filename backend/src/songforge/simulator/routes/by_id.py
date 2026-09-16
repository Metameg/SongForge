"""``GET /byId`` — status + freshly reissued audio URL (issue #11 acceptance #4).

Needed so the url-expires-before-ingest fault is testable: a stale ``conversion_path`` from
the webhook expires, and only a call here reissues a fresh one. Field names are a
documented, internally-consistent placeholder (see ``schemas.py``); they reconcile with the
real client when the ingest issue lands. Behavior lands in the next TDD phase.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from songforge.simulator.schemas import ByIdResponse

router = APIRouter()


@router.get("/byId")
async def by_id(task_id: str) -> ByIdResponse:
    raise HTTPException(status_code=501, detail="simulator /byId not implemented yet")
