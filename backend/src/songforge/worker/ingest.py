"""Worker-side ingest loop: LISTEN/NOTIFY wake-ups over
`jobs/ingest.ingest_claimed_job` (issue #13, acceptance criteria #1-#4).

Wakes on the ingest channel (`POST /api/generation/webhook`,
`web/routes/webhook.py`, NOTIFYs after recording a successful webhook) via a direct
`asyncpg` connection that bypasses any pooler (spec #74), mirroring
`worker/dispatch.py`. The poll interval is only a backstop for a missed
notification.

Split into a testable drain step (`_drain_ingest_pending`, pure DI over its
collaborators) and thin I/O wiring (`run_ingest`, the real LISTEN/NOTIFY connection
+ resilient loop) -- mirrors `worker/dispatch.py` exactly. Like `run_dispatch`,
`run_ingest` itself has no dedicated unit test: any error inside it is logged and
backed off, never raised, so a broken ingest loop never takes down the worker's
other supervised loops -- resilience is its main correctness property.

Row-claimable, not leader-elected (`.orchestrator/CONTEXT.md` "Worker role
gating"): every worker instance runs this loop; `claim_next_ingest_job`'s
`FOR UPDATE SKIP LOCKED` serializes concurrent claims. Unlike dispatch's two-phase
claim, the claiming transaction is held open for the duration of one job's
download+upload (see `jobs/ingest.py::claim_next_ingest_job`'s docstring) -- each
`_drain_ingest_pending` iteration uses its own short-lived session scoped to exactly
one claimed job, so that held transaction never spans more than one job.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from songforge.config import Settings
from songforge.db import get_sessionmaker
from songforge.jobs.generation_client import GenerationClient, HttpGenerationClient
from songforge.jobs.ingest import (
    Downloader,
    HttpAudioDownloader,
    claim_next_ingest_job,
    ingest_claimed_job,
)
from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, Semaphore
from songforge.logging_setup import get_logger
from songforge.metrics import semaphore_released_total
from songforge.models import JOB_STATE_FAILED, JOB_STATE_READY
from songforge.redis_client import get_redis
from songforge.storage import ObjectStorage

log = get_logger(__name__)

# Called with a song's id once its owning job reaches READY in THIS commit (issue #14,
# criterion #3's enqueue-time wake). Optional (defaults to a no-op) -- production wires
# a real `pg_notify(settings.radio_ready_channel, song_id)` the same way
# `worker/dispatch.py`'s `_notify_release` wires `jobs.dispatch.NotifyReleaseFn`.
NotifyReadyFn = Callable[[str], Awaitable[None]]


async def _safe_notify_ready(notify_ready: NotifyReadyFn, song_id: str) -> None:
    """Best-effort post-commit notify (mirror `dispatch._safe_notify` /
    `webhook._safe_notify`): the READY transition (and its `playback_queue` enqueue)
    is already committed, so a NOTIFY failure must never surface -- worst case is the
    radio coordinator's poll/boundary latency, never a lost or duplicated ingest."""
    try:
        await notify_ready(song_id)
    except Exception:
        log.exception("song_ready_notify_failed", song_id=song_id)


async def _safe_release_semaphore(semaphore: Semaphore, user_id: str) -> None:
    """Best-effort generation-semaphore release (issue #16 slot-leak fix): a job
    that reaches READY or FAILED here has left `ACTIVE_JOB_STATES`, so the slot
    `jobs.dispatch.dispatch_claimed_job` acquired before the generation API call must
    be freed. Fired AFTER the commit, same "Redis blip must never abort an
    otherwise-committed Postgres transition" convention as `jobs.dispatch._safe_release`
    -- worst case a missed release is corrected later by the watchdog's reconcile
    backstop (`worker/watchdog.py`)."""
    try:
        await semaphore.release(user_id)
        semaphore_released_total.labels(site="ingest").inc()
    except Exception:
        log.exception("semaphore_release_failed", site="ingest", user_id=user_id)


async def _drain_ingest_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    generation_client: GenerationClient,
    downloader: Downloader,
    settings: Settings,
    notify_ready: NotifyReadyFn | None = None,
    semaphore: Semaphore | None = None,
) -> None:
    """Ingest every currently-claimable job, one at a time, until none remain. Each
    claim + ingest + commit happens in its own session so the row lock (and the
    transaction held for the download+upload) is scoped to exactly one job.

    ``notify_ready`` is optional and backward-compatible (issue #14, criterion #3):
    when a job reaches READY in this commit (and is thereby enqueued onto
    ``playback_queue`` by ``jobs.ingest._finalize_ready``), a POST-COMMIT best-effort
    NOTIFY -- called with the song's id -- wakes the radio coordinator's interrupt path
    -- mirrors ``web/routes/webhook.py``'s "commit first, notify after" ordering (a
    missed notify only costs the coordinator's poll/boundary latency, never
    correctness).

    ``semaphore`` is likewise optional/backward-compatible (issue #16 slot-leak fix):
    when a job reaches READY *or* FAILED in this commit -- i.e. it has left
    ``ACTIVE_JOB_STATES`` -- its generation-semaphore slot is released, POST-COMMIT,
    best-effort. A job requeued back to INGEST_PENDING (the in-claim retry wasn't
    resolved) is still active and must NOT release.
    """
    while True:
        async with sessionmaker() as session:
            job = await claim_next_ingest_job(session)
            if job is None:
                return

            await ingest_claimed_job(
                job,
                session=session,
                storage=storage,
                generation_client=generation_client,
                downloader=downloader,
                settings=settings,
            )
            await session.commit()
            # Only touch `job.state`/`job.song_id`/`job.user_id` when the
            # corresponding hook was actually supplied -- callers that don't care
            # about these wake-ups (including tests exercising this loop with a
            # minimal fake `Job` stand-in) needn't provide those attributes.
            ready_song_id = (
                job.song_id
                if notify_ready is not None and job.state == JOB_STATE_READY
                else None
            )
            release_user_id = (
                job.user_id
                if semaphore is not None and job.state in (JOB_STATE_READY, JOB_STATE_FAILED)
                else None
            )

        if notify_ready is not None and ready_song_id is not None:
            await _safe_notify_ready(notify_ready, ready_song_id)
        if semaphore is not None and release_user_id is not None:
            await _safe_release_semaphore(semaphore, release_user_id)


async def _wait_any(stop: asyncio.Event, wake: asyncio.Event) -> None:
    stop_task = asyncio.ensure_future(stop.wait())
    wake_task = asyncio.ensure_future(wake.wait())
    try:
        await asyncio.wait([stop_task, wake_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, wake_task):
            if not task.done():
                task.cancel()


async def run_ingest(settings: Settings, stop: asyncio.Event) -> None:
    """Run the LISTEN/NOTIFY-driven ingest loop until `stop` is set."""
    sessionmaker = get_sessionmaker()
    # Built from the passed-in `settings` (not the module-level `get_storage()`
    # lru_cache singleton) so this loop honors whatever settings the caller wired up
    # -- important for tests that construct `Settings` with test-only S3 config.
    storage = ObjectStorage.from_settings(settings)
    # Mirrors `worker/dispatch.py::run_dispatch`'s own semaphore wiring -- release
    # site for the slot-leak fix (issue #16): a job dispatch acquired before the
    # generation API call is only released, on this loop's side, once it reaches
    # READY/FAILED here.
    semaphore = RedisSemaphore(RedisSemaphoreBackend(get_redis()), settings)
    # asyncpg wants a plain postgres DSN; the app-wide URL carries the `+asyncpg`
    # SQLAlchemy driver tag, which asyncpg.connect() doesn't understand.
    dsn = settings.database_url.replace("+asyncpg", "")

    while not stop.is_set():
        try:
            listen_conn = await asyncpg.connect(dsn)
            try:
                # Separate connection for the outbound `song_ready` NOTIFY (issue #14,
                # criterion #3) -- mirrors `worker/dispatch.py`'s `notify_conn`, kept
                # distinct from `listen_conn` above (which only ever LISTENs).
                notify_conn = await asyncpg.connect(dsn)
                try:
                    wake = asyncio.Event()

                    def _on_wake(*_args: object) -> None:
                        wake.set()

                    await listen_conn.add_listener(settings.ingest_channel, _on_wake)
                    log.info("ingest_listening", channel=settings.ingest_channel)

                    async def _notify_ready(song_id: str) -> None:
                        await notify_conn.execute(
                            "SELECT pg_notify($1, $2)",
                            settings.radio_ready_channel,
                            song_id,
                        )

                    async with httpx.AsyncClient(
                        timeout=settings.ingest_download_timeout_seconds
                    ) as http_client:
                        generation_client = HttpGenerationClient(settings, http_client)
                        downloader = HttpAudioDownloader(
                            http_client,
                            timeout=settings.ingest_download_timeout_seconds,
                            max_bytes=settings.ingest_max_download_bytes,
                        )
                        while not stop.is_set():
                            await _drain_ingest_pending(
                                sessionmaker,
                                storage,
                                generation_client,
                                downloader,
                                settings,
                                _notify_ready,
                                semaphore,
                            )
                            wake.clear()
                            try:
                                await asyncio.wait_for(
                                    _wait_any(stop, wake),
                                    timeout=settings.ingest_poll_backstop_seconds,
                                )
                            except asyncio.TimeoutError:
                                pass  # backstop poll tick
                finally:
                    await notify_conn.close()
            finally:
                await listen_conn.close()
        except Exception:
            log.exception("ingest_loop_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.ingest_poll_backstop_seconds
                )
            except asyncio.TimeoutError:
                pass  # back off, then retry the LISTEN connection
