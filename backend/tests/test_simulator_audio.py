"""Served fake audio: `GET conversion_path` returns audio bytes (issue #11 acceptance #5),
and the url-expires-before-ingest fault serves an expired URL until refreshed via `/byId`
(acceptance #3/#4).
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


async def test_served_audio_returns_bytes_on_happy_path(rig: SimulatorRig) -> None:
    resp = await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert resp.status_code == 200

    await wait_for_pending_webhooks(rig.app)
    assert len(rig.receiver.received) == 1
    payload = rig.receiver.received[0]

    audio_resp = await rig.client.get(str(payload["conversion_path"]))
    assert audio_resp.status_code == 200
    assert audio_resp.content
    assert audio_resp.headers["content-type"].startswith("audio/")


async def test_url_expires_before_ingest_serves_expired_until_byid_refresh(
    rig: SimulatorRig,
) -> None:
    create_resp = await rig.client.post(
        CREATE_PATH,
        json=DEFAULT_BODY,
        headers={FAULT_HEADER: Fault.URL_EXPIRES_BEFORE_INGEST.value},
    )
    assert create_resp.status_code == 200
    task_id = create_resp.json()["task_id"]

    await wait_for_pending_webhooks(rig.app)
    assert len(rig.receiver.received) == 1
    stale_payload = rig.receiver.received[0]

    stale_resp = await rig.client.get(str(stale_payload["conversion_path"]))
    assert stale_resp.status_code in (403, 410)  # expired/forbidden until refreshed

    refreshed = await rig.client.get("/byId", params={"task_id": task_id})
    assert refreshed.status_code == 200
    fresh_url = refreshed.json()["audio_url"]
    assert fresh_url and fresh_url != stale_payload["conversion_path"]

    fresh_resp = await rig.client.get(fresh_url)
    assert fresh_resp.status_code == 200
    assert fresh_resp.content
