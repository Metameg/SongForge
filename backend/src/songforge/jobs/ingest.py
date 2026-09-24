"""Ingest decision: a claimed INGEST_PENDING job -> download -> R2 upload -> READY
(issue #13, acceptance criteria #2/#3/#4).

Mirrors ``songforge.jobs.dispatch``'s split exactly: ``claim_next_ingest_job`` is the
``FOR UPDATE SKIP LOCKED`` DB claim (``worker/ingest.py``'s wiring calls it);
``ingest_claimed_job`` is the pure-ish decision step, taking an already-claimed
``Job`` (state ``INGEST_PENDING``) plus injected ports (``storage``, ``downloader``,
``generation_client``) so the retry/refresh/exhaustion logic is unit-testable
without any live datastore or network.

Unlike ``dispatch_claimed_job``, this step also creates the ``Song`` row (the
"ordinary playable object" acceptance criterion #3 requires) -- so it additionally
takes the claiming ``session`` to stage that insert (``session.add``, no explicit
flush/commit -- the caller, ``worker/ingest.py``'s drain loop, owns the commit,
exactly like dispatch's).

Retry shape (criterion #4): the webhook's ``audio_url`` is only a *hint* URL that
can expire before ingest runs. On a download failure, this refreshes the URL via
the generation API's by-id lookup and retries once immediately, in the same claim.
Only a failure of *that* refreshed attempt (or of the by-id lookup itself) requeues
the job (INGEST_PENDING with backoff) for a fresh claim pass later, up to
``settings.ingest_max_attempts`` before giving up -> FAILED. This is NOT the
(out-of-scope, later) watchdog's periodic overdue-ingest sweep -- it is the bounded
retry *within* one ingest attempt.

Idempotent re-ingest (criterion #2's immutable key): the object key is derived from
``job.conversion_id_1`` (the canonical conversion the PRD says to store -- see
plan-issue-13.md "song_id derivation"), known before any download happens, so
``storage.exists`` is checked FIRST -- a re-claim after a crash between upload and
the READY commit skips the download and upload entirely, and a re-claim after the
Song row was already created (but before the job's own READY commit) does not
attempt a duplicate insert.

Playback-queue enqueue on READY is a later ticket (see ``.orchestrator/CONTEXT.md``
OUT-of-scope) -- this module stops at a READY job with an ordinary playable ``Song``
row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GenerationClient,
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
)
from songforge.logging_setup import get_logger
from songforge.metrics import (
    ingest_completed_total,
    ingest_failed_total,
    ingest_requeued_total,
)
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    SOURCE_GENERATED,
    Job,
    Song,
)
from songforge.storage import ObjectStorage, audio_key

log = get_logger(__name__)


class AudioDownloadError(Exception):
    """The audio URL could not be fetched (network error, timeout, or a non-200
    response -- an expired token included). Triggers the by-id refresh-and-retry."""


class Downloader(Protocol):
    """Port ``ingest_claimed_job`` depends on for fetching audio bytes; tests inject
    a stub/fake."""

    async def download(self, url: str) -> bytes:
        """Fetch the audio bytes at ``url``. Raises ``AudioDownloadError`` on any
        network error, timeout, or non-200 response (e.g. the simulator's 403 on an
        expired token)."""
        ...


class HttpAudioDownloader:
    """Real downloader: a plain ``httpx`` GET -- no auth header, the URL itself is
    the capability (matches the simulator's ``/audio/{token}`` contract). Honors
    ``settings.ingest_download_timeout_seconds`` via the constructor so every call
    through this instance uses the same configured timeout."""

    def __init__(self, http_client: httpx.AsyncClient, *, timeout: float) -> None:
        self._http_client = http_client
        self._timeout = timeout

    async def download(self, url: str) -> bytes:
        try:
            response = await self._http_client.get(url, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise AudioDownloadError(f"audio download timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise AudioDownloadError(f"audio download request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AudioDownloadError(
                f"audio download returned {response.status_code}"
            )
        return response.content


async def _download_with_refresh(
    job: Job, downloader: Downloader, generation_client: GenerationClient
) -> bytes | None:
    """Try the job's current (possibly expired) hint URL; on failure, refresh it via
    the by-id lookup and retry exactly once. Returns ``None`` (never raises) if
    every avenue is exhausted -- the caller decides requeue-vs-fail from that."""
    assert job.audio_url is not None  # INGEST_PENDING implies the webhook set this
    assert job.task_id is not None  # set alongside conversion_id_1 by dispatch

    try:
        return await downloader.download(job.audio_url)
    except AudioDownloadError:
        log.info("ingest_download_failed_refreshing", job_id=job.job_id)

    try:
        fresh_url = await generation_client.get_audio_url_by_id(job.task_id)
    except (GenerationRateLimited, GenerationRejected, GenerationTransientError) as exc:
        log.warning("ingest_by_id_refresh_failed", job_id=job.job_id, error=str(exc))
        return None

    job.audio_url = fresh_url
    try:
        return await downloader.download(fresh_url)
    except AudioDownloadError:
        log.warning("ingest_refreshed_download_failed", job_id=job.job_id)
        return None


async def _finalize_ready(session: AsyncSession, job: Job, song_id: str, key: str) -> None:
    """Set the job READY + song_id, creating the ``Song`` row if it doesn't already
    exist (idempotent re-claim: a crash before the READY commit but after the Song
    insert must not attempt a duplicate insert)."""
    existing_song = await session.get(Song, song_id)
    if existing_song is None:
        duration = int(job.audio_duration) if job.audio_duration is not None else None
        session.add(
            Song(
                id=song_id,
                title=job.title or "Untitled",
                source=SOURCE_GENERATED,
                object_key=key,
                duration_seconds=duration,
            )
        )
    job.state = JOB_STATE_READY
    job.song_id = song_id


async def ingest_claimed_job(
    job: Job,
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    generation_client: GenerationClient,
    downloader: Downloader,
    settings: Settings,
) -> None:
    """Drive one claimed (``state == INGEST_PENDING``) job through download -> R2
    upload -> READY, or requeue/fail on exhaustion.

    Mutates ``job`` (and stages a ``Song`` insert via ``session.add``) in place; the
    caller (``worker/ingest.py``) owns the DB commit -- mirrors
    ``jobs.dispatch.dispatch_claimed_job``'s "pure decision, caller commits"
    convention.
    """
    if job.conversion_id_1 is None:
        # Data-integrity guard: WAITING_FOR_WEBHOOK -> INGEST_PENDING should never
        # happen without conversion_id_1 (dispatch sets it before that state), but
        # this must never crash the ingest loop if it somehow does.
        job.state = JOB_STATE_FAILED
        ingest_failed_total.inc()
        log.error("ingest_missing_conversion_id", job_id=job.job_id)
        return

    song_id = job.conversion_id_1
    key = audio_key(song_id)

    if storage.exists(key):
        # Self-heal: the object is already in R2 (a prior claim uploaded it but
        # crashed before committing READY) -- skip the download/upload entirely.
        await _finalize_ready(session, job, song_id, key)
        ingest_completed_total.inc()
        log.info("ingest_completed_idempotent", job_id=job.job_id, song_id=song_id)
        return

    audio_bytes = await _download_with_refresh(job, downloader, generation_client)

    if audio_bytes is None:
        job.ingest_attempts += 1
        if job.ingest_attempts >= settings.ingest_max_attempts:
            job.state = JOB_STATE_FAILED
            ingest_failed_total.inc()
            log.info(
                "ingest_failed_exhausted", job_id=job.job_id, attempts=job.ingest_attempts
            )
        else:
            job.state = JOB_STATE_INGEST_PENDING
            job.available_at = datetime.now(timezone.utc) + timedelta(
                seconds=settings.ingest_requeue_backoff_seconds
            )
            ingest_requeued_total.inc()
            log.info(
                "ingest_requeued", job_id=job.job_id, attempts=job.ingest_attempts
            )
        return

    storage.put(key, audio_bytes)
    await _finalize_ready(session, job, song_id, key)
    ingest_completed_total.inc()
    log.info("ingest_completed", job_id=job.job_id, song_id=song_id)


async def claim_next_ingest_job(session: AsyncSession) -> Job | None:
    """Claim the oldest available INGEST_PENDING job.

    ``FOR UPDATE SKIP LOCKED`` lets every worker instance run this concurrently
    without double-claiming, ordered by ``seq`` (the partial index
    ``ix_jobs_ingest_pending_seq``, migration ``0004_ingest``). Unlike
    ``dispatch.claim_next_job``, this does NOT commit an intermediate state
    transition before the caller's slow work runs -- there is no intermediate state
    to move to (``INGEST_PENDING -> READY/FAILED`` is direct); the transaction (and
    the row lock) is held for the duration of the download+upload instead. Accepted
    trade-off (see ``plan-issue-13.md`` "Held transaction during ingest"), bounded
    by ``settings.ingest_download_timeout_seconds``, in the same spirit as
    ``worker/radio_coordinator.py`` holding its advisory lock for its own lifetime.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(Job.state == JOB_STATE_INGEST_PENDING, Job.available_at <= now)
        .order_by(Job.seq)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.scalars(stmt)).first()
