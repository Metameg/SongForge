"""`POST /api/generation/webhook` -- record the finished generation, flip the job to
INGEST_PENDING, and NOTIFY the ingest worker (issue #13, acceptance criterion #1).

Thin and fast: this handler does no downloading and no object-storage I/O -- it only
records the webhook's metadata (audio URL hint, duration, title) and flips one job's
state, so the generation API's webhook call never times out and retries (PRD #6,
"webhook is thin"). Idempotent: a duplicate or late webhook for a job that has already
left WAITING_FOR_WEBHOOK (INGEST_PENDING/READY/FAILED) is a no-op 200 (PRD #6's
duplicate-webhook idempotency requirement; exercised by the simulator's
DUPLICATE_WEBHOOK/DELAYED_WEBHOOK faults). An unknown ``task_id`` is also a no-op 200,
not a 404 -- a non-2xx here would only invite the external API to retry something this
service can never act on.

A failure-status payload (or a success-shaped payload missing the one field ingest
needs, ``conversion_path``) marks the job FAILED. Quota refund + user SSE notification
on failure is a LATER ticket (see ``.orchestrator/CONTEXT.md`` OUT-of-scope) -- this
handler only flips the state; no NOTIFY fires on this path since there is no ingest
work to wake a worker for.

Mirrors ``web/routes/create.py``'s DI shape (``get_session`` / notify dependency) so
HTTP-edge tests can substitute an in-memory SQLite session and a NOTIFY spy.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.db import get_sessionmaker
from songforge.logging_setup import get_logger
from songforge.metrics import webhooks_received_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)

router = APIRouter(tags=["webhook"])
log = get_logger(__name__)

# The ingest-wake NOTIFY hook's shape: ``notify(channel, payload)``. Kept as a plain
# callable (not a class) so tests can override the dependency with a trivial spy.
Notifier = Callable[[str, str], Awaitable[None]]


class WebhookPayload(BaseModel):
    """Body POSTed by the generation API on completion (or failure) -- field names
    mirror the simulator's contract (``simulator/schemas.py::WebhookPayload``).
    ``conversion_path`` is the (possibly expiring) audio URL hint; ``status`` is only
    present on an error/failed delivery."""

    subtype: str = "music_ai"
    task_id: str
    conversion_id: str
    conversion_path: str | None = None
    conversion_duration: float | None = None
    title: str | None = None
    status: str | None = None


class WebhookResponse(BaseModel):
    status: str = "ok"


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Per-request DB session; overridden in tests via ``app.dependency_overrides``."""
    async with get_sessionmaker()() as session:
        yield session


async def _pg_notify(session: AsyncSession, channel: str, payload: str) -> None:
    """Default notifier: ``SELECT pg_notify(...)`` on the request's own connection,
    committed immediately -- a NOTIFY queued inside a transaction that never commits
    is never delivered."""
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": payload},
    )
    await session.commit()


async def _safe_notify(notify: Notifier, channel: str, payload: str) -> None:
    """Best-effort post-commit notify (quality report LOW finding, mirrors
    ``jobs.dispatch._safe_notify``): the state transition above is already durably
    committed by this point, so a NOTIFY failure (e.g. a dropped connection between
    commit and this call) must never surface as a 500 to the external generation
    API's webhook caller -- that would incorrectly signal failure and invite a retry,
    which the idempotency check above already handles as a no-op. Worst case here is
    only the poll backstop's latency, never a lost transition."""
    try:
        await notify(channel, payload)
    except Exception:
        log.exception("webhook_notify_failed", channel=channel, payload=payload)


def get_notify_dependency(
    session: AsyncSession = Depends(get_session),
) -> Notifier:
    """The ingest-wake NOTIFY hook; overridden in tests with a spy that records
    ``(channel, payload)`` calls instead of touching Postgres."""

    async def _notify(channel: str, payload: str) -> None:
        await _pg_notify(session, channel, payload)

    return _notify


@router.post("/api/generation/webhook", response_model=WebhookResponse)
async def receive_webhook(
    body: WebhookPayload,
    session: AsyncSession = Depends(get_session),
    notify: Notifier = Depends(get_notify_dependency),
) -> WebhookResponse:
    """Look up the job by ``task_id``, transition it, NOTIFY, and return 200.

    Ordering is the point of this handler (mirrors ``create.py``'s equivalent test):
    the transition is committed to Postgres before ``notify`` fires, and this route
    never calls the generation client -- the audio download/upload belongs entirely
    to the async ingest worker (``songforge.jobs.ingest``).

    SECURITY (deferred, follow-up ticket -- see security report Finding 1): this
    route does not verify the caller is genuinely the generation provider (no HMAC
    signature / shared-secret / source check). Left unbuilt for #13 because the
    simulator seam has no signing contract to verify against and the topology is
    currently internal-only + UUID-gated (``task_id`` is a 128-bit random handle) --
    but a real external provider deployment MUST add provider-signature
    verification here (``identity.py``'s ``hmac.compare_digest`` pattern is the
    model) before this endpoint is genuinely public. The audio URL this handler
    records is trusted downstream at the actual network fetch -- see the matching
    comment at the download site in ``songforge.jobs.ingest.HttpAudioDownloader``.
    """
    settings = get_settings()
    # `.with_for_update()` (LOW/MED finding): without a row lock, two genuinely
    # concurrent deliveries for the same `task_id` could both observe
    # WAITING_FOR_WEBHOOK before either commits and both apply the transition +
    # NOTIFY. Harmless today (ingest's own claim is FOR UPDATE SKIP LOCKED-
    # serialized, so a second wake just finds nothing to do), but this closes the
    # race at its source instead of relying on that downstream idempotency. A no-op
    # on SQLite (the dialect used by this file's unit tests) -- it simply doesn't
    # emit a locking clause there, matching `jobs.dispatch.claim_next_job`'s
    # precedent for the same reason.
    job = (
        await session.scalars(
            select(Job).where(Job.task_id == body.task_id).with_for_update()
        )
    ).first()

    if job is None:
        webhooks_received_total.labels(outcome="unknown_task").inc()
        log.info("webhook_unknown_task_id", task_id=body.task_id)
        return WebhookResponse()

    if job.state != JOB_STATE_WAITING_FOR_WEBHOOK:
        # Already ingested/failed (or, defensively, not yet dispatched) -- a
        # duplicate or late-arriving webhook. PRD #6 idempotency: never re-run the
        # transition or NOTIFY again.
        webhooks_received_total.labels(outcome="duplicate_ignored").inc()
        log.info("webhook_duplicate_ignored", task_id=body.task_id, state=job.state)
        return WebhookResponse()

    if body.status is not None or body.conversion_path is None:
        # A declared failure, or a success-shaped payload missing the one field
        # ingest needs -- either way unrecoverable without a real audio URL. Quota
        # refund + user SSE notify on failure is a LATER ticket (see
        # `.orchestrator/CONTEXT.md` OUT-of-scope); this only flips the state.
        job.state = JOB_STATE_FAILED
        await session.commit()
        webhooks_received_total.labels(outcome="failed").inc()
        log.info("webhook_marked_failed", task_id=body.task_id, status=body.status)
        return WebhookResponse()

    job.audio_url = body.conversion_path
    job.audio_duration = body.conversion_duration
    job.title = body.title
    job.state = JOB_STATE_INGEST_PENDING
    await session.commit()
    webhooks_received_total.labels(outcome="ingest_pending").inc()
    log.info("webhook_ingest_pending", task_id=body.task_id, job_id=job.job_id)

    await _safe_notify(notify, settings.ingest_channel, job.job_id)

    return WebhookResponse()
