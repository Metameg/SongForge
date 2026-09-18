"""Pydantic request/response contracts mirroring the real MusicGPT API (issue #11).

Field names match what the old Flask client (``app/home/services.py::MusicAPIClient``) and
webhook handler (``app/home/api.py::webhook``) key off, so swapping the real API for this
simulator is purely a base-URL change. Exact ``/byId`` field names aren't documented
upstream; this shape is a documented, internally-consistent placeholder and will be
reconciled with the real client when the ingest issue lands (see ``.orchestrator/CONTEXT.md``).
"""

from __future__ import annotations

import enum

from pydantic import AnyHttpUrl, BaseModel


class CreateRequest(BaseModel):
    """Body of ``POST /api/public/v1/MusicAI``."""

    prompt: str
    lyrics: str | None = None
    make_instrumental: bool = False
    vocal_only: bool = False
    # Validated as an http(s) URL so an invalid/placeholder value (e.g. Swagger's "string"
    # default) is rejected at create with a clear 422, rather than blowing up the detached
    # webhook-delivery task later. Stored as a plain str on the task record.
    webhook_url: AnyHttpUrl


class CreateResponse(BaseModel):
    """Synchronous 200 response from the create call — handles known at submit time."""

    task_id: str
    conversion_id_1: str
    conversion_id_2: str
    eta: int
    credit_estimate: float


class WebhookPayload(BaseModel):
    """Body POSTed later to the request's ``webhook_url`` on completion (or failure)."""

    subtype: str = "music_ai"
    task_id: str
    conversion_id: str
    conversion_path: str | None = None
    conversion_duration: float | None = None
    title: str | None = None
    # Present on the error/failed fault paths; absent on the happy path.
    status: str | None = None


class ByIdStatus(str, enum.Enum):
    """Status enum returned by ``/byId`` (issue #11: IN_QUEUE -> COMPLETED/ERROR/FAILED)."""

    IN_QUEUE = "IN_QUEUE"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    FAILED = "FAILED"


class ByIdResponse(BaseModel):
    """Body of ``GET /byId``: current status plus a freshly (re)issued audio URL."""

    task_id: str
    status: ByIdStatus
    conversion_id_1: str
    conversion_id_2: str
    audio_url: str | None = None
    conversion_duration: float | None = None
    title: str | None = None
