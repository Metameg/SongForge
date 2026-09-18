"""Create-call contract: `POST /api/public/v1/MusicAI` synchronously returns handles
(issue #11 acceptance #1). In-process ASGI client only — no real network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from songforge.simulator.app import create_app
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, make_sim_client


@pytest.fixture()
async def sim_client() -> AsyncIterator[httpx.AsyncClient]:
    async with make_sim_client(create_app()) as client:
        yield client


async def test_create_returns_200_with_all_expected_fields(
    sim_client: httpx.AsyncClient,
) -> None:
    resp = await sim_client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    for field in (
        "task_id",
        "conversion_id_1",
        "conversion_id_2",
        "eta",
        "credit_estimate",
    ):
        assert field in body


async def test_create_field_types(sim_client: httpx.AsyncClient) -> None:
    resp = await sim_client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["task_id"], str)
    assert isinstance(body["conversion_id_1"], str)
    assert isinstance(body["conversion_id_2"], str)
    assert isinstance(body["eta"], int)
    assert isinstance(body["credit_estimate"], (int, float))


async def test_create_ids_are_distinct_and_nonempty(sim_client: httpx.AsyncClient) -> None:
    resp = await sim_client.post(CREATE_PATH, json=DEFAULT_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"]
    assert body["conversion_id_1"]
    assert body["conversion_id_2"]
    assert body["conversion_id_1"] != body["conversion_id_2"]


async def test_create_accepts_real_request_body_shape(sim_client: httpx.AsyncClient) -> None:
    resp = await sim_client.post(
        CREATE_PATH,
        json={
            "prompt": "an upbeat pop anthem",
            "lyrics": "la la la",
            "make_instrumental": False,
            "vocal_only": True,
            "webhook_url": "http://webhook.test/webhook",
        },
    )
    assert resp.status_code == 200
