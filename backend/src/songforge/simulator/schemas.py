"""Pydantic request/response contracts mirroring the real MusicGPT API (issue #11).

Reconciled with the real MusicGPT contract (docs.musicgpt.com/api-documentation:
MusicAI create, byId status check, webhook callback), so swapping the real API for this
simulator stays purely a base-URL change (``MUSICGPT_BASE_URL``). The shapes the client
(``jobs/generation_client.py``) and webhook route (``web/routes/webhook.py``) parse are
the ones modelled here:

- create response: ``task_id`` + ``conversion_id_1`` + ``conversion_id_2`` + ``eta`` +
  ``credit_estimate`` at the top level;
- ``/byId``: ``{"success", "conversion": {...}}`` -- the task fields are NESTED under
  ``conversion`` (``status``/``audio_url``/``title``...), not top-level;
- webhook: ``success`` is the failure signal (``false`` on error/failed); there is no
  ``status`` field on the webhook body (status lives only on the ``/byId`` poll).
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
    """Body POSTed later to the request's ``webhook_url`` on completion (or failure).

    ``success`` is the real MusicGPT failure signal: ``true`` on a completed generation,
    ``false`` on error/failed. There is no ``status`` field on the webhook (that lives on
    the ``/byId`` poll only)."""

    subtype: str = "music_ai"
    task_id: str
    conversion_id: str
    conversion_path: str | None = None
    conversion_duration: float | None = None
    title: str | None = None
    album_cover_path: str | None = None
    # True on the happy path; False on the error/failed fault paths.
    success: bool = True


class ByIdStatus(str, enum.Enum):
    """Status enum returned by ``/byId`` (issue #11: IN_QUEUE -> COMPLETED/ERROR/FAILED)."""

    IN_QUEUE = "IN_QUEUE"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    FAILED = "FAILED"


class ByIdConversion(BaseModel):
    """The task record nested under ``ByIdResponse.conversion`` -- the real MusicGPT
    ``/byId`` shape. Only the fields the client reads (``status``/``audio_url``/
    ``title``) plus the task/conversion handles are modelled; the real response also
    carries ``status_msg``/``conversion_cost``/``lyrics``/``music_style``/timestamps,
    which this service ignores. Note the single ``conversion_id`` (not ``_1``/``_2``)
    and the absence of a duration field here -- duration rides the webhook."""

    task_id: str
    conversion_id: str
    status: ByIdStatus
    audio_url: str | None = None
    title: str | None = None


class ByIdResponse(BaseModel):
    """Body of ``GET /api/public/v1/byId``: a ``success`` flag plus the task fields
    NESTED under ``conversion`` (the real MusicGPT shape)."""

    success: bool = True
    conversion: ByIdConversion
