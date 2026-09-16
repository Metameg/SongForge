"""Shared test rig for the MusicGPT simulator (issue #11).

Everything here keeps simulator tests off real network/time:
- `WebhookReceiver` is a tiny in-process ASGI app that records every POST it gets.
- `make_webhook_client` / `make_sim_client` wire httpx to those in-process apps via
  `httpx.ASGITransport` — no real sockets, regardless of the host in a URL.
- `make_recording_sleep` / `make_gated_sleep` replace the simulator's real delay with a
  fake that either resolves instantly (recording what delay was requested) or blocks until
  the test releases it — so both "the webhook was delivered" and "the webhook is still
  pending" are asserted deterministically, with no real `asyncio.sleep`.

Not a test module itself (no `test_` prefix), so pytest won't collect it.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

import httpx
from fastapi import FastAPI, Request

from songforge.simulator.app import create_app
from songforge.simulator.clock import SleepFn


class WebhookReceiver:
    """In-process ASGI app standing in for the pipeline's real webhook endpoint."""

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []
        self.app = FastAPI()

        @self.app.post("/webhook")
        async def _receive(request: Request) -> dict[str, str]:
            self.received.append(await request.json())
            return {"status": "received"}


def make_webhook_client(receiver: WebhookReceiver) -> httpx.AsyncClient:
    """An httpx client that routes *any* URL to the in-process receiver app.

    `httpx.ASGITransport` means requests never touch the network regardless of the host in
    `webhook_url` — the simulator can use a realistic-looking URL.
    """
    transport = httpx.ASGITransport(app=receiver.app)
    return httpx.AsyncClient(transport=transport, base_url="http://webhook.test")


def make_sim_client(app: FastAPI, base_url: str = "http://sim.test") -> httpx.AsyncClient:
    """An httpx client that drives the simulator app in-process, no real network."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=base_url)


def make_recording_sleep() -> tuple[SleepFn, list[float]]:
    """A fake `sleep_fn` that records the requested delay and returns immediately."""
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)

    return _sleep, calls


def make_gated_sleep() -> tuple[SleepFn, asyncio.Event]:
    """A fake `sleep_fn` that blocks until the test calls `gate.set()`.

    Lets a test observe the "still pending" (not-yet-delivered) state deterministically —
    the scheduled webhook task is left suspended on the gate until the test says otherwise.
    """
    gate = asyncio.Event()

    async def _sleep(seconds: float) -> None:
        await gate.wait()

    return _sleep, gate


class SimulatorRig(NamedTuple):
    """A simulator app wired to an in-process webhook receiver and a recording delay."""

    app: FastAPI
    client: httpx.AsyncClient
    receiver: WebhookReceiver
    delay_calls: list[float]


def make_rig() -> SimulatorRig:
    receiver = WebhookReceiver()
    sleep_fn, delay_calls = make_recording_sleep()
    app = create_app(http_client=make_webhook_client(receiver), sleep_fn=sleep_fn)
    client = make_sim_client(app)
    return SimulatorRig(app=app, client=client, receiver=receiver, delay_calls=delay_calls)


CREATE_PATH = "/api/public/v1/MusicAI"

DEFAULT_BODY: dict[str, object] = {
    "prompt": "a chill lofi beat",
    "make_instrumental": True,
    "vocal_only": False,
    "webhook_url": "http://webhook.test/webhook",
}

# Re-exported for type-hinting convenience in test modules.
__all__ = [
    "CREATE_PATH",
    "DEFAULT_BODY",
    "SimulatorRig",
    "WebhookReceiver",
    "make_gated_sleep",
    "make_recording_sleep",
    "make_rig",
    "make_sim_client",
    "make_webhook_client",
]
