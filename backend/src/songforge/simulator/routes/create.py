"""``POST /api/public/v1/MusicAI`` — the real MusicGPT create-call contract (issue #11).

Behavior (synchronous handle issuance, delay/fault selection via the ``X-Sim-Fault`` /
``X-Sim-Delay-Seconds`` headers, scheduling the completion webhook) lands in the next TDD
phase. This stub only wires the route + request/response schema so tests fail on assertions
about that behavior, not on 404s or import errors.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from songforge.simulator.schemas import CreateRequest, CreateResponse

router = APIRouter()


@router.post("/api/public/v1/MusicAI")
async def create_music(body: CreateRequest) -> CreateResponse:
    raise HTTPException(status_code=501, detail="simulator create not implemented yet")
