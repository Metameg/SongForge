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
from songforge.metrics import bot_check_failed_total, jobs_created_total
from songforge.models import JOB_STATE_QUEUED, Job
from songforge.redis_client import get_redis
from songforge.web.bot_check import BotCheck, HeaderTokenBotCheck
from songforge.web.identity import client_ip, resolve_identity, set_identity_cookie
from songforge.web.rate_limit import RateLimiter, RedisRateLimitBackend

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


def get_rate_limiter() -> RateLimiter:
    """The daily-quota rate limiter (issue #15, criteria #1/#4); overridden in tests with
    a fake so the edge tests need no live Redis. Reads settings at request time so the
    ``enforce_rate_limits`` kill switch (and test monkeypatches of ``get_settings``) take
    effect without reconstructing the app."""
    settings = get_settings()
    return RateLimiter(RedisRateLimitBackend(get_redis()), settings)


def get_bot_check() -> BotCheck:
    """The bot-check gate (issue #15, criterion #3); overridden in tests with a
    pass/fail fake."""
    return HeaderTokenBotCheck(get_settings())


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
    rate_limiter: RateLimiter = Depends(get_rate_limiter),
    bot_check: BotCheck = Depends(get_bot_check),
) -> CreateResponse:
    """Gate the create action, then persist QUEUED before any external call, then NOTIFY.

    Gate order (issue #15): the **bot check** runs first (403 on failure -- deter
    scripted farming before doing any work); then prompt **validation** (422 -- reject
    malformed input WITHOUT charging a quota slot); then the **daily-quota rate limiter**
    ``consume`` (429 on any exceeded cap -- charges the slot only for a valid, human
    request). Every rejection persists nothing.

    After the gates, the ordering from issue #12 still holds (criteria #1 + #5): the job
    row is committed to Postgres, in the QUEUED state, before ``notify`` fires and long
    before any generation-API call (that call belongs entirely to
    ``songforge.jobs.dispatch``, never to this route).
    """
    settings = get_settings()

    identity = resolve_identity(request, settings)
    ip = client_ip(request, settings)

    if not await bot_check.verify(request):
        bot_check_failed_total.inc()
        log.info("bot_check_failed", user_id=identity.user_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bot check failed")

    _validate_prompt(body.prompt)
    _validate_lyrics(body.lyrics)
    lyrics = body.lyrics.strip() if body.lyrics else None

    decision = await rate_limiter.consume(identity, ip)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "daily song limit reached — try again tomorrow",
        )

    job = Job(
        job_id=uuid.uuid4().hex,
        user_id=identity.user_id,
        prompt=body.prompt.strip(),
        lyrics=lyrics,
        state=JOB_STATE_QUEUED,
        webhook_url=settings.musicgpt_webhook_url,
        # Issue #16, design D3: persisted so the watchdog's terminal-failure sweep can
        # reconstruct the EXACT identity + ip that created this job for an accurate
        # `RateLimiter.refund` -- an anon create charges BOTH the cookie and ip
        # counters, so the ip leg here is required, not redundant with `user_id`.
        client_ip=ip,
        is_authenticated=identity.is_authenticated,
    )
    session.add(job)
    await session.commit()
    jobs_created_total.inc()
    log.info("job_created", job_id=job.job_id, user_id=identity.user_id)

    await notify(settings.jobs_new_channel, job.job_id)

    if identity.minted:
        set_identity_cookie(response, identity.user_id, settings)

    return CreateResponse(job_id=job.job_id, state=job.state)
