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

Robustness (phase-5 review hardening): the whole decision body below the
``conversion_id_1`` guard runs under a catch-all (mirrors
``dispatch.dispatch_claimed_job``'s own catch-all) so any unmapped exception --
a blocking-storage-call error, a flush-time integrity error, or any other surprise --
bounds-retries/fails the one job instead of propagating and wedging the whole ingest
loop (every worker always re-claims the oldest ``seq`` first). The synchronous
``ObjectStorage`` calls (``exists``/``put``) are offloaded via ``asyncio.to_thread``
so a slow/blocked S3 call can't freeze this loop's event loop -- and, with it, the
heartbeat and radio-coordinator clock sharing that same loop.
"""

from __future__ import annotations

import asyncio
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
    through this instance uses the same configured timeout.

    Streams the response body and aborts once ``max_bytes`` is exceeded (security
    report MED finding: an unbounded ``response.content`` read on a hostile/oversized
    ``audio_url`` could OOM the worker) -- a hostile/oversized/exceeded-cap response
    is treated exactly like any other download failure (``AudioDownloadError``),
    which routes through the normal bounded-retry -> requeue/FAILED path, not a
    special case.

    SECURITY (deferred, follow-up ticket -- see security report Finding 2): ``url``
    is not scheme/host-validated before this GET -- no https-only rule, no allowlist
    against the known provider/CDN domain. Left unbuilt for #13 because an https-only
    rule would break the plain-``http://`` dev simulator this URL currently always
    points at, and a real allowlist needs the real provider's/CDN's domain(s), which
    the simulator seam doesn't have. Gated behind closing the matching webhook-auth
    gap (``web/routes/webhook.py``'s ``receive_webhook`` docstring) -- ``url`` here
    is exactly the ``audio_url`` a forged webhook could control, so an SSRF
    scheme/host allowlist belongs alongside that hardening, before any real external
    provider deployment. (Redirect-based SSRF is already closed: ``httpx`` defaults
    to ``follow_redirects=False``.)
    """

    def __init__(
        self, http_client: httpx.AsyncClient, *, timeout: float, max_bytes: int
    ) -> None:
        self._http_client = http_client
        self._timeout = timeout
        self._max_bytes = max_bytes

    async def download(self, url: str) -> bytes:
        try:
            async with self._http_client.stream(
                "GET", url, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    raise AudioDownloadError(
                        f"audio download returned {response.status_code}"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise AudioDownloadError(
                            f"audio download exceeded max size of "
                            f"{self._max_bytes} bytes"
                        )
                return bytes(body)
        except httpx.TimeoutException as exc:
            raise AudioDownloadError(f"audio download timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise AudioDownloadError(f"audio download request failed: {exc}") from exc


async def _download_with_refresh(
    job: Job,
    downloader: Downloader,
    generation_client: GenerationClient,
    *,
    audio_url: str,
    task_id: str,
) -> bytes | None:
    """Try the job's current (possibly expired) hint URL; on failure, refresh it via
    the by-id lookup and retry exactly once. Returns ``None`` (never raises) if
    every avenue is exhausted -- the caller decides requeue-vs-fail from that.

    Takes ``audio_url``/``task_id`` as explicit, non-``None`` parameters rather than
    reading ``job.audio_url``/``job.task_id`` directly: the caller
    (``ingest_claimed_job``) already guards both for ``None`` before calling this, so
    passing the narrowed values lets mypy check this function without a bare
    ``assert`` (quality report HIGH finding: an ``assert`` is silently stripped under
    ``python -O``, so a violation would have raised uncaught instead of failing the
    job gracefully)."""
    try:
        return await downloader.download(audio_url)
    except AudioDownloadError:
        log.info("ingest_download_failed_refreshing", job_id=job.job_id)

    try:
        fresh_url = await generation_client.get_audio_url_by_id(task_id)
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
    insert must not attempt a duplicate insert).

    Flushes the staged ``Song`` INSERT *before* mutating ``job.state``/``job.song_id``
    (prod-validation CRITICAL finding): ``Job.song_id`` and ``Song`` have no declared
    ``relationship()`` between them, so SQLAlchemy's unit-of-work has no dependency
    information telling it to order the ``Song`` INSERT ahead of the ``Job`` UPDATE at
    commit time. Without this explicit flush, the two can flush in the wrong order and
    trip the ``fk_jobs_song_id_songs`` FK constraint on real Postgres -- reproduced
    deterministically against a live database. SQLite does not enforce foreign keys by
    default, so every ingest test running against an in-memory SQLite session was
    blind to this."""
    existing_song = await session.get(Song, song_id)
    if existing_song is None:
        duration = round(job.audio_duration) if job.audio_duration is not None else None
        session.add(
            Song(
                id=song_id,
                title=job.title or "Untitled",
                source=SOURCE_GENERATED,
                object_key=key,
                duration_seconds=duration,
            )
        )
        await session.flush()
    job.state = JOB_STATE_READY
    job.song_id = song_id


def _requeue_or_fail(job: Job, settings: Settings) -> None:
    """Shared "bump attempts, then requeue-with-backoff or give up" step, used by both
    the download-exhaustion path and the catch-all below -- a failed ingest attempt is
    a failed ingest attempt regardless of which port raised."""
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
        log.info("ingest_requeued", job_id=job.job_id, attempts=job.ingest_attempts)


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

    Everything below the ``conversion_id_1`` guard runs under a catch-all (quality
    report CRITICAL finding, mirrors ``dispatch_claimed_job``'s own catch-all): any
    unmapped exception -- a ``ClientError`` from a blocking storage call, a flush-time
    integrity error, or any other surprise -- must never propagate out of this
    function. ``claim_next_ingest_job`` always claims the oldest ``seq``, so an
    uncaught exception here would permanently head-of-line-block every ingest job
    behind this one, on every worker instance (row-claimable, not leader-elected),
    until someone manually fixes the row.
    """
    if not job.conversion_id_1:
        # Data-integrity guard: WAITING_FOR_WEBHOOK -> INGEST_PENDING should never
        # happen without a non-empty conversion_id_1 (dispatch sets it before that
        # state), but this must never crash the ingest loop if it somehow does.
        # Falsiness (not `is None`) is deliberate -- quality report MED finding: an
        # empty-string conversion_id_1 must be rejected too, or it would sail through
        # as a "valid" id (`Song(id="")`, key `audio/.mp3`).
        job.state = JOB_STATE_FAILED
        ingest_failed_total.inc()
        log.error("ingest_missing_conversion_id", job_id=job.job_id)
        return

    song_id = job.conversion_id_1
    key = audio_key(song_id)

    try:
        # Offloaded via `asyncio.to_thread` (quality report MED finding): `storage`
        # wraps a synchronous boto3 client, and calling it directly here would block
        # this coroutine's event loop -- shared with the heartbeat and radio-
        # coordinator clock in `worker/main.py`'s supervised `gather` -- for the
        # duration of the S3 round trip.
        if await asyncio.to_thread(storage.exists, key):
            # Self-heal: the object is already in R2 (a prior claim uploaded it but
            # crashed before committing READY) -- skip the download/upload entirely.
            await _finalize_ready(session, job, song_id, key)
            ingest_completed_total.inc()
            log.info("ingest_completed_idempotent", job_id=job.job_id, song_id=song_id)
            return

        if job.audio_url is None or job.task_id is None:
            # Data-integrity guard mirroring the conversion_id_1 one above: an
            # INGEST_PENDING job should always have both set (the webhook route sets
            # audio_url before this state; dispatch sets task_id alongside
            # conversion_id_1). Previously enforced by a bare `assert` in
            # `_download_with_refresh` (quality report HIGH finding) -- stripped
            # under `python -O`, and would have raised uncaught rather than failing
            # this one job gracefully.
            job.state = JOB_STATE_FAILED
            ingest_failed_total.inc()
            log.error("ingest_missing_audio_url_or_task_id", job_id=job.job_id)
            return

        audio_bytes = await _download_with_refresh(
            job,
            downloader,
            generation_client,
            audio_url=job.audio_url,
            task_id=job.task_id,
        )

        if audio_bytes is None:
            _requeue_or_fail(job, settings)
            return

        await asyncio.to_thread(storage.put, key, audio_bytes)
        await _finalize_ready(session, job, song_id, key)
        ingest_completed_total.inc()
        log.info("ingest_completed", job_id=job.job_id, song_id=song_id)
    except Exception:
        # Catch-all: see the module/function docstrings above. Roll back any partial
        # flush (e.g. a Song insert rejected by a column constraint -- security
        # report MED "poison-pill" finding: an oversized `title` must count toward
        # the attempt cap, not retry forever) so the session is clean for the
        # caller's own `session.commit()`, then bound the retry exactly like the
        # download-failure path above.
        log.exception("ingest_unmapped_error", job_id=job.job_id)
        await session.rollback()
        _requeue_or_fail(job, settings)


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
