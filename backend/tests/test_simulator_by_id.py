"""`/api/public/v1/byId` lookup: status enum (IN_QUEUE -> COMPLETED/ERROR/FAILED) + a
fresh audio URL (issue #11 acceptance #4 — needed so url-expires-before-ingest is
testable). Matches the real MusicGPT contract: the task fields are nested under a
`conversion` object, and both `task_id` and `conversionType` are required query params.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from songforge.simulator.app import create_app, wait_for_pending_webhooks
from tests.simulator_helpers import (
    BYID_PATH,
    CREATE_PATH,
    DEFAULT_BODY,
    SimulatorRig,
    WebhookReceiver,
    byid_params,
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

        status_resp = await client.get(BYID_PATH, params=byid_params(task_id))
        assert status_resp.status_code == 200
        assert status_resp.json()["conversion"]["status"] == "IN_QUEUE"

        gate.set()
        await wait_for_pending_webhooks(app)


async def test_by_id_reports_completed_with_fresh_audio_url_after_delivery(
    rig: SimulatorRig,
) -> None:
    create_resp = await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert create_resp.status_code == 200
    body = create_resp.json()

    await wait_for_pending_webhooks(rig.app)

    status_resp = await rig.client.get(BYID_PATH, params=byid_params(body["task_id"]))
    assert status_resp.status_code == 200
    conversion = status_resp.json()["conversion"]
    assert conversion["status"] == "COMPLETED"
    assert conversion["conversion_path_1"]  # real byId's audio field (not `audio_url`)
    assert conversion["conversion_id_1"] == body["conversion_id_1"]


async def test_by_id_withholds_audio_url_and_title_until_completed() -> None:
    receiver = WebhookReceiver()
    sleep_fn, gate = make_gated_sleep()
    app = create_app(http_client=make_webhook_client(receiver), sleep_fn=sleep_fn)

    async with make_sim_client(app) as client:
        task_id = (await client.post(CREATE_PATH, json=DEFAULT_BODY)).json()["task_id"]

        before = (await client.get(BYID_PATH, params=byid_params(task_id))).json()
        assert before["conversion"]["status"] == "IN_QUEUE"
        assert before["conversion"]["conversion_path_1"] is None
        assert before["conversion"]["title_1"] is None

        gate.set()
        await wait_for_pending_webhooks(app)

        after = (await client.get(BYID_PATH, params=byid_params(task_id))).json()
        assert after["conversion"]["status"] == "COMPLETED"
        assert after["conversion"]["conversion_path_1"]
        assert after["conversion"]["title_1"]


async def test_by_id_unknown_task_id_is_404(rig: SimulatorRig) -> None:
    resp = await rig.client.get(BYID_PATH, params=byid_params("does-not-exist"))
    assert resp.status_code == 404
