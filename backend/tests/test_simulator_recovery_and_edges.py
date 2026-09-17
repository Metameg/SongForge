"""Edge cases and the recovery contracts the faults exist to enable (issue #11).

These assert *why* the fault switches are there: a lost webhook is still recoverable by
polling `/byId`, an unknown audio token is rejected, and an unrecognised fault selector is a
safe no-op (happy path). All observed through the HTTP surface, no internals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from songforge.simulator.app import wait_for_pending_webhooks
from songforge.simulator.faults import FAULT_HEADER, Fault
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, SimulatorRig, make_rig


@pytest.fixture()
async def rig() -> AsyncIterator[SimulatorRig]:
    r = make_rig()
    async with r.client:
        yield r


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
