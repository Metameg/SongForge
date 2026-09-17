"""FastAPI application factory for the fault-injectable MusicGPT simulator (issue #11).

A separate, standalone ASGI app with no dependency on the main songforge web app or any
datastore — the whole pipeline is pointed at it (or the real MusicGPT API) purely by
base-URL config (``MUSICGPT_BASE_URL``). Mirrors :func:`songforge.web.app.create_app`'s
shape and observability conventions.

Behavior (fault logic, timing, handle bookkeeping) lands in the next TDD phase; this factory
wires the app, its injection seams, and route registration so tests fail on assertions, not
on import errors or 404s.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from songforge import __version__
from songforge.logging_setup import configure_logging, get_logger
from songforge.simulator.clock import SleepFn, real_sleep
from songforge.simulator.config import SimulatorSettings, get_settings
from songforge.simulator.routes import audio, by_id, create
from songforge.simulator.store import TaskStore


def create_app(
    settings: SimulatorSettings | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    sleep_fn: SleepFn | None = None,
) -> FastAPI:
    """Build the simulator app.

    ``http_client`` and ``sleep_fn`` are test injection seams: a client wired to an
    in-process webhook receiver (e.g. via ``httpx.ASGITransport``), and a fake delay
    recorder/gate, so webhook delivery and its timing are asserted deterministically —
    without real network calls or real sleeps.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Drain in-flight webhook deliveries and close the HTTP client we own on shutdown.
        # (Runs only under an ASGI server; httpx.ASGITransport in tests doesn't fire it, and
        # tests inject + close their own client, so there's no double-close.)
        yield
        await wait_for_pending_webhooks(app)
        if owns_http_client:
            await app.state.http_client.aclose()

    owns_http_client = http_client is None

    app = FastAPI(
        title="MusicGPT Simulator",
        version=__version__,
        summary="Fault-injectable fake of the MusicGPT generation API (dev/test only).",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.http_client = http_client or httpx.AsyncClient()
    app.state.sleep_fn = sleep_fn or real_sleep
    # In-memory task registry (per app, so each create_app() is isolated) — the seam create,
    # the delayed webhook, /byId, and audio serving all share.
    app.state.store = TaskStore()
    # Completion webhooks are delivered by fire-and-forget asyncio tasks scheduled from the
    # create endpoint (never blocking the synchronous create response). Tracked here so
    # tests can await delivery to finish deterministically via `wait_for_pending_webhooks`.
    app.state.pending_webhook_tasks = set()

    app.include_router(create.router)
    app.include_router(by_id.router)
    app.include_router(audio.router)

    get_logger(__name__).info("simulator_app_created", version=__version__)
    return app


async def wait_for_pending_webhooks(app: FastAPI) -> None:
    """Await every currently in-flight scheduled webhook delivery to completion.

    Test-only seam: deterministically drives delayed webhook delivery to completion —
    typically paired with a fake, instantly-resolving ``sleep_fn`` — without a real wait.
    """
    tasks: set[asyncio.Task[None]] = app.state.pending_webhook_tasks
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = create_app()
