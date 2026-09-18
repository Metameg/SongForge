"""``POST /api/public/v1/MusicAI`` — the real MusicGPT create-call contract (issue #11).

Returns the generation handles (``task_id`` + both ``conversion_id``s + ``eta`` +
``credit_estimate``) *synchronously* — handles-known-at-submit is what makes later by-handle
(``/byId``) polling possible — then schedules the completion webhook out of band. The
``X-Sim-Fault`` header selects a fault and ``X-Sim-Delay-Seconds`` overrides the webhook delay
(``0`` = instant mode).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from songforge.simulator.delivery import resolve_delay, schedule_webhook
from songforge.simulator.faults import DELAY_HEADER, FAULT_HEADER, Fault
from songforge.simulator.schemas import CreateRequest, CreateResponse
from songforge.simulator.store import TaskRecord

router = APIRouter()

# Synchronous create-response stand-ins (the real API returns an estimate, not a promise).
_ETA_SECONDS = 60
_CREDIT_ESTIMATE = 1.0
_SAMPLE_DURATION_SECONDS = 120.0


def _parse_delay(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@router.post("/api/public/v1/MusicAI")
async def create_music(body: CreateRequest, request: Request) -> CreateResponse:
    fault = Fault.from_header(request.headers.get(FAULT_HEADER))

    # A 429 is a clean, retriable rejection: nothing is charged, no handle is issued, and no
    # webhook is ever scheduled (mirrors the real API's concurrency backstop, PRD #18).
    if fault is Fault.RATE_LIMIT_429:
        raise HTTPException(
            status_code=429, detail="high demand — retriable, no charge"
        )

    delay_override = _parse_delay(request.headers.get(DELAY_HEADER))
    settings = request.app.state.settings
    store = request.app.state.store

    task_id = uuid.uuid4().hex
    conversion_id_1 = uuid.uuid4().hex
    conversion_id_2 = uuid.uuid4().hex
    record = TaskRecord(
        task_id=task_id,
        conversion_id_1=conversion_id_1,
        conversion_id_2=conversion_id_2,
        prompt=body.prompt,
        webhook_url=str(body.webhook_url),
        fault=fault,
        duration=_SAMPLE_DURATION_SECONDS,
        title=f"[SIM] {body.prompt[:50]}",
    )
    store.add(record)

    schedule_webhook(request.app, record, resolve_delay(settings, fault, delay_override))

    return CreateResponse(
        task_id=task_id,
        conversion_id_1=conversion_id_1,
        conversion_id_2=conversion_id_2,
        eta=_ETA_SECONDS,
        credit_estimate=_CREDIT_ESTIMATE,
    )
