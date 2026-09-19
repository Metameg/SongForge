"""``POST /create`` — mint the job, persist QUEUED before any external call (issue #12).

Criterion #1: establishes a signed-cookie anon identity if absent (reused, with no
cookie churn, if a valid one is already present); the job is persisted as QUEUED
BEFORE any external call — this route never talks to the generation API, that is
``songforge.jobs.dispatch``'s job entirely (see ``.orchestrator/CONTEXT.md`` "Explicitly
OUT of scope"). After persist, ``pg_notify``s the new-job channel so a listening
dispatcher wakes without polling (criterion #5).

Dependency-injection mirrors ``now_playing.py`` (``get_session``) so HTTP-edge tests can
substitute an in-memory SQLite session and a NOTIFY spy without touching Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.db import get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.metrics import jobs_created_total
from songforge.models import JOB_STATE_QUEUED, Job
from songforge.web.identity import mint, sign, unsign

router = APIRouter(tags=["create"])
log = get_logger(__name__)

# The new-job NOTIFY hook's shape: ``notify(channel, payload)``. Kept as a plain
# callable (not a class) so tests can override the dependency with a trivial spy.
Notifier = Callable[[str, str], Awaitable[None]]


class CreateRequest(BaseModel):
    """``POST /create`` body."""

    prompt: str
    lyrics: str | None = None


class CreateResponse(BaseModel):
    """``POST /create`` response — the job handle the client polls/subscribes on."""

    job_id: str
    state: str


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


async def _pg_notify(session: AsyncSession, channel: str, payload: str) -> None:
    """Default notifier: ``SELECT pg_notify(...)`` on the request's own connection,
    committed immediately -- a NOTIFY queued inside a transaction that never commits
    (e.g. the session closing without another explicit commit) is never delivered."""
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": payload},
    )
    await session.commit()


def get_notify_dependency(
    session: AsyncSession = Depends(get_session),
) -> Notifier:
    """The new-job NOTIFY hook (criterion #5); overridden in tests with a spy that
    records ``(channel, payload)`` calls instead of touching Postgres."""

    async def _notify(channel: str, payload: str) -> None:
        await _pg_notify(session, channel, payload)

    return _notify


def _read_identity(request: Request) -> str | None:
    """Read + verify the signed identity cookie, if present. ``None`` on absent,
    malformed, or tampered — the caller mints a fresh identity in that case."""
    settings = get_settings()
    raw = request.cookies.get(settings.identity_cookie_name)
    if raw is None:
        return None
    return unsign(raw, secret=settings.session_secret)


def _set_identity_cookie(response: Response, user_id: str) -> None:
    """Mint the signed cookie for a freshly-minted identity (criterion #1).

    `secure` is config-driven off `environment` (security report MEDIUM finding):
    this cookie is the sole identity/auth token, so outside local dev (where plain
    HTTP is expected) it must never be sent over an unencrypted connection.
    """
    settings = get_settings()
    response.set_cookie(
        settings.identity_cookie_name,
        sign(user_id, secret=settings.session_secret),
        max_age=settings.identity_cookie_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "local",
    )


def _validate_prompt(prompt: str) -> None:
    """422 on an empty/whitespace-only or oversized prompt (criterion #1)."""
    settings = get_settings()
    stripped = prompt.strip()
    if not stripped:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "prompt must not be empty")
    if len(prompt) > settings.prompt_max_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"prompt exceeds max length ({settings.prompt_max_length})",
        )


def _validate_lyrics(lyrics: str | None) -> None:
    """422 on oversized lyrics -- mirrors `_validate_prompt`'s length cap (security
    report MEDIUM finding: `lyrics` is the equally attacker-controlled sibling
    field and had no cap at all)."""
    if lyrics is None:
        return
    settings = get_settings()
    if len(lyrics) > settings.lyrics_max_length:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"lyrics exceeds max length ({settings.lyrics_max_length})",
        )


@router.post("/create", response_model=CreateResponse)
async def create_job(
    body: CreateRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    notify: Notifier = Depends(get_notify_dependency),
) -> CreateResponse:
    """Mint/reuse identity, validate the prompt, persist QUEUED, then NOTIFY.

    Ordering is the point of this handler (criteria #1 + #5): the job row is committed
    to Postgres, in the QUEUED state, before ``notify`` fires and long before any
    generation-API call happens (that call belongs entirely to
    ``songforge.jobs.dispatch``, never to this route).
    """
    settings = get_settings()
    _validate_prompt(body.prompt)
    _validate_lyrics(body.lyrics)
    lyrics = body.lyrics.strip() if body.lyrics else None

    existing_identity = _read_identity(request)
    user_id = existing_identity if existing_identity is not None else mint()

    job = Job(
        job_id=uuid.uuid4().hex,
        user_id=user_id,
        prompt=body.prompt.strip(),
        lyrics=lyrics,
        state=JOB_STATE_QUEUED,
        webhook_url=settings.musicgpt_webhook_url,
    )
    session.add(job)
    await session.commit()
    jobs_created_total.inc()
    log.info("job_created", job_id=job.job_id, user_id=user_id)

    await notify(settings.jobs_new_channel, job.job_id)

    if existing_identity is None:
        _set_identity_cookie(response, user_id)

    return CreateResponse(job_id=job.job_id, state=job.state)
