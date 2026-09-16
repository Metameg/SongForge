"""Completion webhook delivery: configurable delay + instant mode (issue #11 acceptance #2).

The webhook fires via an asyncio task the create endpoint schedules; `wait_for_pending_webhooks`
awaits that same task deterministically — driven by an injected fake `sleep_fn` — so timing
is asserted without any real `asyncio.sleep` wait.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from songforge.simulator.app import wait_for_pending_webhooks
from songforge.simulator.faults import DELAY_HEADER
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, SimulatorRig, make_rig


@pytest.fixture()
async def rig() -> AsyncIterator[SimulatorRig]:
    r = make_rig()
    async with r.client:
        yield r


async def test_webhook_delivered_after_configured_delay(rig: SimulatorRig) -> None:
    resp = await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert resp.status_code == 200
    body = resp.json()

    await wait_for_pending_webhooks(rig.app)

    assert rig.delay_calls == [5.0]  # simulator default (SimulatorSettings.webhook_delay_seconds)
    assert len(rig.receiver.received) == 1
    payload = rig.receiver.received[0]
    assert payload["subtype"] == "music_ai"
    assert payload["task_id"] == body["task_id"]
    assert payload["conversion_id"] in (body["conversion_id_1"], body["conversion_id_2"])
    assert payload["conversion_path"]
    assert "conversion_duration" in payload
    assert "title" in payload


async def test_instant_mode_delivers_webhook_without_real_wait(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={DELAY_HEADER: "0"}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)

    assert rig.delay_calls == [0.0]
    assert len(rig.receiver.received) == 1


async def test_custom_delay_header_overrides_default(rig: SimulatorRig) -> None:
    resp = await rig.client.post(
        CREATE_PATH, json=DEFAULT_BODY, headers={DELAY_HEADER: "12.5"}
    )
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)

    assert rig.delay_calls == [12.5]
