"""`/byId` lookup: status enum (IN_QUEUE -> COMPLETED/ERROR/FAILED) + a fresh audio URL
(issue #11 acceptance #4 — needed so url-expires-before-ingest is testable).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from songforge.simulator.app import create_app, wait_for_pending_webhooks
from tests.simulator_helpers import (
    CREATE_PATH,
    DEFAULT_BODY,
    SimulatorRig,
    WebhookReceiver,
    make_gated_sleep,
    make_rig,
    make_sim_client,
    make_webhook_client,
)


@pytest.fixture()
async def rig() -> AsyncIterator[SimulatorRig]:
    r = make_rig()
    async with r.client:
        yield r


async def test_by_id_reports_in_queue_before_delivery() -> None:
    # A gated sleep_fn never resolves until the test releases it, so the scheduled webhook
    # task is deterministically still pending when we poll /byId — no real wait involved.
    receiver = WebhookReceiver()
    sleep_fn, gate = make_gated_sleep()
    app = create_app(http_client=make_webhook_client(receiver), sleep_fn=sleep_fn)

    async with make_sim_client(app) as client:
        create_resp = await client.post(CREATE_PATH, json=DEFAULT_BODY)
        assert create_resp.status_code == 200
        task_id = create_resp.json()["task_id"]

        status_resp = await client.get("/byId", params={"task_id": task_id})
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "IN_QUEUE"

        gate.set()
        await wait_for_pending_webhooks(app)


async def test_by_id_reports_completed_with_fresh_audio_url_after_delivery(
    rig: SimulatorRig,
) -> None:
    create_resp = await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert create_resp.status_code == 200
    body = create_resp.json()

    await wait_for_pending_webhooks(rig.app)

    status_resp = await rig.client.get("/byId", params={"task_id": body["task_id"]})
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "COMPLETED"
    assert status_body["audio_url"]
    assert status_body["conversion_id_1"] == body["conversion_id_1"]
    assert status_body["conversion_id_2"] == body["conversion_id_2"]


async def test_by_id_unknown_task_id_is_404(rig: SimulatorRig) -> None:
    resp = await rig.client.get("/byId", params={"task_id": "does-not-exist"})
    assert resp.status_code == 404
