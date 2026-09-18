"""Fault-injection switches selected via the `X-Sim-Fault` header (issue #11 acceptance #3).

Each switch is asserted through externally observable behavior only: the create response,
and what the in-process webhook receiver observes (or doesn't) — never internal call counts.
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


async def test_webhook_never_arrives_create_still_succeeds_but_no_webhook_delivered(
    rig: SimulatorRig,
) -> None:
    resp = await rig.client.post(
        CREATE_PATH,
        json=DEFAULT_BODY,
        headers={FAULT_HEADER: Fault.WEBHOOK_NEVER_ARRIVES.value},
    )
    assert resp.status_code == 200
    assert resp.json()["task_id"]

    await wait_for_pending_webhooks(rig.app)
    assert rig.receiver.received == []


async def test_error_fault_reports_error_status_via_webhook(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.ERROR.value}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)

    assert len(rig.receiver.received) == 1
    assert rig.receiver.received[0]["status"] == "ERROR"


async def test_failed_fault_reports_failed_status_via_byid(rig: SimulatorRig) -> None:
    create_resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.FAILED.value}
    )
    assert create_resp.status_code == 200
    task_id = create_resp.json()["task_id"]

    await wait_for_pending_webhooks(rig.app)

    status_resp = await rig.client.get("/byId", params={"task_id": task_id})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "FAILED"


async def test_delayed_webhook_uses_longer_than_default_delay(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.DELAYED_WEBHOOK.value}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)

    assert len(rig.receiver.received) == 1
    assert rig.delay_calls[0] > 5.0  # longer than the happy-path default


async def test_duplicate_webhook_delivers_identical_payload_twice(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.DUPLICATE_WEBHOOK.value}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)

    assert len(rig.receiver.received) == 2
    assert rig.receiver.received[0] == rig.receiver.received[1]


async def test_rate_limit_429_fault_rejects_create(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={FAULT_HEADER: Fault.RATE_LIMIT_429.value}
    )
    assert resp.status_code == 429

    await wait_for_pending_webhooks(rig.app)
    assert rig.receiver.received == []
