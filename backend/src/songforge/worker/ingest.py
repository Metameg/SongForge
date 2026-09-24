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
from songforge.logging_setup import get_logger
from songforge.storage import ObjectStorage

log = get_logger(__name__)


async def _drain_ingest_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
    storage: ObjectStorage,
    generation_client: GenerationClient,
    downloader: Downloader,
    settings: Settings,
) -> None:
    """Ingest every currently-claimable job, one at a time, until none remain. Each
    claim + ingest + commit happens in its own session so the row lock (and the
    transaction held for the download+upload) is scoped to exactly one job.
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
    # asyncpg wants a plain postgres DSN; the app-wide URL carries the `+asyncpg`
    # SQLAlchemy driver tag, which asyncpg.connect() doesn't understand.
    dsn = settings.database_url.replace("+asyncpg", "")

    while not stop.is_set():
        try:
            listen_conn = await asyncpg.connect(dsn)
            try:
                wake = asyncio.Event()

                def _on_wake(*_args: object) -> None:
                    wake.set()

                await listen_conn.add_listener(settings.ingest_channel, _on_wake)
                log.info("ingest_listening", channel=settings.ingest_channel)

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
                            sessionmaker, storage, generation_client, downloader, settings
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
                await listen_conn.close()
        except Exception:
            log.exception("ingest_loop_error")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.ingest_poll_backstop_seconds
                )
            except asyncio.TimeoutError:
                pass  # back off, then retry the LISTEN connection
