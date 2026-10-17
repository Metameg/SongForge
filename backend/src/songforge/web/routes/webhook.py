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

import hmac
from collections.abc import AsyncGenerator, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.correlation import bind_correlation_id
from songforge.db import get_sessionmaker
from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, Semaphore
from songforge.logging_setup import get_logger
from songforge.metrics import semaphore_released_total, webhooks_received_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Job,
)
from songforge.radio.user_events import (
    PublishUserEventFn,
    progress_message,
    redis_user_event_publisher,
    safe_publish_user_event,
)
from songforge.redis_client import get_redis

router = APIRouter(tags=["webhook"])
log = get_logger(__name__)

# The ingest-wake NOTIFY hook's shape: ``notify(channel, payload)``. Kept as a plain
# callable (not a class) so tests can override the dependency with a trivial spy.
Notifier = Callable[[str, str], Awaitable[None]]


class WebhookPayload(BaseModel):
    """Body POSTed by the generation API on completion (or failure) -- field names
    mirror the real MusicGPT contract (docs.musicgpt.com; the simulator mirrors it in
    ``simulator/schemas.py::WebhookPayload``). ``conversion_path`` is the (possibly
    expiring) audio URL hint; ``success`` is the failure signal -- the real API posts
    ``success: false`` on an error/failed generation (there is NO ``status`` field on
    the webhook; status is only exposed via the ``/byId`` poll). Defaults to ``None``
    when absent so a payload that simply omits it is NOT treated as a declared failure.
    Unlisted real fields (``conversion_path_wav``, ``is_flagged``, ``reason``,
    ``lyrics``...) are ignored by pydantic's default ``extra``."""

    subtype: str = "music_ai"
    task_id: str
    conversion_id: str
    conversion_path: str | None = None
    conversion_duration: float | None = None
    title: str | None = None
    album_cover_path: str | None = None
    success: bool | None = None


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


def get_publish_user_event_dependency() -> PublishUserEventFn:
    """The per-user SSE publisher (issue #37); overridden in tests with a spy. Built
    from ``get_settings()``/``get_redis()`` per request (``Redis.from_url`` is lazy,
    so this does no I/O until the publish itself)."""
    return redis_user_event_publisher(
        get_redis(), get_settings().user_events_channel_prefix
    )


def get_semaphore_dependency() -> Semaphore:
    """The generation semaphore (issue #16 slot-leak fix); overridden in tests with a
    fake so edge tests need no live Redis. Mirrors ``web/routes/create.py``'s
    ``get_rate_limiter`` shape: built fresh per request from ``get_settings()`` so a
    test's monkeypatched settings take effect without reconstructing the app."""
    return RedisSemaphore(RedisSemaphoreBackend(get_redis()), get_settings())


async def _safe_release_semaphore(semaphore: Semaphore, user_id: str) -> None:
    """Best-effort post-commit semaphore release (issue #16 slot-leak fix, mirrors
    ``_safe_notify`` above): the FAILED transition below is already durably
    committed, so a release failure (e.g. a dropped Redis connection) must never
    surface as a 500 to the external generation API's webhook caller. Worst case is
    corrected later by the watchdog's reconcile backstop (``worker/watchdog.py``)."""
    try:
        await semaphore.release(user_id)
        semaphore_released_total.labels(site="webhook").inc()
    except Exception:
        log.exception("semaphore_release_failed", site="webhook", user_id=user_id)


def require_webhook_token(token: str | None = Query(default=None)) -> None:
    """Reject a delivery that doesn't carry ``WEBHOOK_SECRET`` as ``?token=`` (dispatch
    appends it to the URL handed to the generation API). Runs before the body is
    trusted or the database is touched. A no-op when no secret is configured."""
    secret = get_settings().webhook_secret
    if not secret:
        return
    if token is None or not hmac.compare_digest(token.encode(), secret.encode()):
        webhooks_received_total.labels(outcome="bad_token").inc()
        log.warning("webhook_bad_token", token_present=token is not None)
        raise HTTPException(status_code=401, detail="invalid webhook token")


@router.post(
    "/api/generation/webhook",
    response_model=WebhookResponse,
    dependencies=[Depends(require_webhook_token)],
)
async def receive_webhook(
    body: WebhookPayload,
    session: AsyncSession = Depends(get_session),
    notify: Notifier = Depends(get_notify_dependency),
    semaphore: Semaphore = Depends(get_semaphore_dependency),
    publish_user_event: PublishUserEventFn = Depends(
        get_publish_user_event_dependency
    ),
) -> WebhookResponse:
    """Look up the job by ``task_id``, transition it, NOTIFY, and return 200.

    Ordering is the point of this handler (mirrors ``create.py``'s equivalent test):
    the transition is committed to Postgres before ``notify`` fires, and this route
    never calls the generation client -- the audio download/upload belongs entirely
    to the async ingest worker (``songforge.jobs.ingest``).

    SECURITY: the route is public in prod (proxied by the frontend), so
    ``require_webhook_token`` checks the shared ``WEBHOOK_SECRET`` echoed back as
    ``?token=``. MusicGPT doesn't sign webhooks, so a shared secret is the strongest
    check available; rotate it by changing the env var. The audio URL this handler
    records is trusted downstream at the actual network fetch -- see the matching
    comment at the download site in ``songforge.jobs.ingest.HttpAudioDownloader``.

    Issue #18, criterion #1: once the job is found, the remainder of this handler
    runs with the JOB's own submit-time ``correlation_id`` re-bound -- deliberately
    overriding this request's own ``CorrelationIdMiddleware``-minted id, since the
    point of the trace is the song's original submit id, not the webhook delivery's
    own transient request id. This is log-only: ``CorrelationIdMiddleware.dispatch``
    already captured its own ``cid`` into a local variable before calling this route
    and echoes THAT variable on the response header, not a fresh contextvar read, so
    overriding the binding here never changes the echoed ``X-Correlation-ID``.
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

    with bind_correlation_id(job.correlation_id):
        if job.state != JOB_STATE_WAITING_FOR_WEBHOOK:
            # Already ingested/failed (or, defensively, not yet dispatched) -- a
            # duplicate or late-arriving webhook. PRD #6 idempotency: never re-run
            # the transition or NOTIFY again.
            webhooks_received_total.labels(outcome="duplicate_ignored").inc()
            log.info(
                "webhook_duplicate_ignored", task_id=body.task_id, state=job.state
            )
            return WebhookResponse()

        if body.success is False or body.conversion_path is None:
            # A declared failure (`success: false`, the real MusicGPT failure signal),
            # or a success-shaped payload missing the one field ingest needs -- either
            # way unrecoverable without a real audio URL. `is False` (not falsy): an
            # absent `success` must NOT fail an otherwise-complete payload. Quota refund
            # + user SSE notify on failure is the watchdog's terminal sweep; this only
            # flips the state (and releases the slot below).
            job.state = JOB_STATE_FAILED
            await session.commit()
            webhooks_received_total.labels(outcome="failed").inc()
            log.info(
                "webhook_marked_failed", task_id=body.task_id, success=body.success
            )
            # Issue #16 slot-leak fix: the job leaves WAITING_FOR_WEBHOOK (an active
            # state) here without ever reaching ingest -- release its generation-
            # semaphore slot, post-commit, best-effort.
            await _safe_release_semaphore(semaphore, job.user_id)
            return WebhookResponse()

        job.audio_url = body.conversion_path
        job.audio_duration = body.conversion_duration
        job.title = body.title
        job.album_cover_path = body.album_cover_path
        job.state = JOB_STATE_INGEST_PENDING
        await session.commit()
        progress = (
            job.user_id,
            progress_message(job.job_id, job.state, job.eta),
        )
        webhooks_received_total.labels(outcome="ingest_pending").inc()
        log.info("webhook_ingest_pending", task_id=body.task_id, job_id=job.job_id)

        await _safe_notify(notify, settings.ingest_channel, job.job_id)
        # Issue #37: tell the creator (and only the creator) the audio is now being
        # ingested; post-commit, best-effort like the NOTIFY above.
        await safe_publish_user_event(
            publish_user_event, *progress, log_event="webhook_publish_user_event_failed"
        )

        return WebhookResponse()
