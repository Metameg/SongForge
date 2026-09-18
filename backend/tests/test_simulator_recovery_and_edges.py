"""Edge cases and the recovery contracts the faults exist to enable (issue #11).

These assert *why* the fault switches are there: a lost webhook is still recoverable by
polling `/byId`, an unknown audio token is rejected, and an unrecognised fault selector is a
safe no-op (happy path). All observed through the HTTP surface, no internals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from songforge.simulator.app import create_app, wait_for_pending_webhooks
from songforge.simulator.faults import FAULT_HEADER, Fault
from tests.simulator_helpers import (
    CREATE_PATH,
    DEFAULT_BODY,
    SimulatorRig,
    make_gated_sleep,
    make_rig,
    make_sim_client,
)


@pytest.fixture()
async def rig() -> AsyncIterator[SimulatorRig]:
    r = make_rig()
    async with r.client:
        yield r


class _RaisingClient:
    """Stand-in webhook client whose POST always fails, like an unreachable receiver."""

    def __init__(self) -> None:
        self.calls = 0

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        raise httpx.ConnectError("simulated delivery failure")


async def test_invalid_webhook_url_is_rejected_at_create_with_422() -> None:
    async with make_sim_client(create_app()) as client:
        resp = await client.post(
            CREATE_PATH, json={**DEFAULT_BODY, "webhook_url": "string"}
        )
    assert resp.status_code == 422


async def test_delivery_failure_never_escapes_the_background_task() -> None:
    # A failing webhook client must be swallowed+logged, not left as an unretrieved-task
    # exception. Gate the delay so the task is still pending when we snapshot it.
    boom = _RaisingClient()
    sleep_fn, gate = make_gated_sleep()
    app = create_app(http_client=boom, sleep_fn=sleep_fn)  # type: ignore[arg-type]

    async with make_sim_client(app) as client:
        resp = await client.post(CREATE_PATH, json=DEFAULT_BODY)
        assert resp.status_code == 200
        tasks = set(app.state.pending_webhook_tasks)
        assert tasks  # the delivery task is parked on the gate
        gate.set()
        await wait_for_pending_webhooks(app)

    assert boom.calls == 1  # delivery was attempted
    for task in tasks:
        assert task.exception() is None  # ...and its failure did not escape


async def test_webhook_never_arrives_is_still_recoverable_via_byid(
    rig: SimulatorRig,
) -> None:
    """The point of the fault: no webhook is delivered, but the generation did complete —
    a by-handle poll (what the watchdog will do) finds it COMPLETED with a servable URL.
    """
    create_resp = await rig.client.post(
        CREATE_PATH,
        json=DEFAULT_BODY,
        headers={FAULT_HEADER: Fault.WEBHOOK_NEVER_ARRIVES.value},
    )
    assert create_resp.status_code == 200
    task_id = create_resp.json()["task_id"]

    await wait_for_pending_webhooks(rig.app)
    assert rig.receiver.received == []  # nothing was delivered

    status_resp = await rig.client.get("/byId", params={"task_id": task_id})
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "COMPLETED"
    assert body["audio_url"]

    audio_resp = await rig.client.get(body["audio_url"])
    assert audio_resp.status_code == 200
    assert audio_resp.content


async def test_unknown_audio_token_is_404(rig: SimulatorRig) -> None:
    resp = await rig.client.get("/audio/never-minted-token")
    assert resp.status_code == 404


async def test_unrecognised_fault_header_falls_back_to_happy_path(
    rig: SimulatorRig,
) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: "not-a-real-fault"}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)
    assert len(rig.receiver.received) == 1
    assert rig.receiver.received[0]["subtype"] == "music_ai"
    assert rig.receiver.received[0]["conversion_path"]


async def test_duplicate_webhook_payload_references_a_servable_url(
    rig: SimulatorRig,
) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.DUPLICATE_WEBHOOK.value}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)
    assert len(rig.receiver.received) == 2
    first = rig.receiver.received[0]
    audio_resp = await rig.client.get(str(first["conversion_path"]))
    assert audio_resp.status_code == 200
