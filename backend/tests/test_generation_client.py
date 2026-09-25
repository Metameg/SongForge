"""Unit tests for the generation API client (issue #12, criterion #4). Exercises
`jobs/generation_client.HttpGenerationClient.create` against `httpx.MockTransport` --
no real network, matching the repo's "all tests mock external services" convention.
Field shape mirrors the issue #11 simulator's contract (`simulator/schemas.py`).

The `httpx.MockTransport` tests above hand-craft the JSON response, so they can't
catch drift between what `HttpGenerationClient.create` parses and what the simulator
(the actual dispatch target, `simulator/routes/create.py` + `simulator/schemas.py`)
really returns -- a hand-authored fixture can invent a shape the real endpoint would
never produce and the test would stay green regardless (data-contract audit, issue
#12 phase 4). The tests below drive the REAL simulator ASGI app (via
`tests/simulator_helpers.make_rig`/`make_sim_client`, `httpx.ASGITransport` -- no real
network) through the same `HttpGenerationClient.create` production code path,
including the real 429 fault (`X-Sim-Fault: rate-limit-429`), so fixture drift on
either side can't hide a break.
"""

from __future__ import annotations

import httpx
import pytest

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GENERATION_STATUS_COMPLETED,
    GENERATION_STATUS_ERROR,
    GENERATION_STATUS_FAILED,
    GENERATION_STATUS_IN_QUEUE,
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
    HttpGenerationClient,
)
from songforge.simulator.app import wait_for_pending_webhooks
from songforge.simulator.faults import FAULT_HEADER, Fault
from tests.simulator_helpers import DEFAULT_BODY, make_rig, make_sim_client


def _settings() -> Settings:
    return Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "MUSICGPT_BASE_URL": "http://musicgpt.test",
            "MUSICGPT_API_KEY": "test-key",
        }
    )


async def test_create_returns_handles_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://musicgpt.test/api/public/v1/MusicAI"
        assert request.headers["authorization"] == "test-key"
        return httpx.Response(
            200,
            json={
                "task_id": "t1", "conversion_id_1": "c1", "conversion_id_2": "c2",
                "eta": 60, "credit_estimate": 1.0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        handles = await client.create(
            prompt="a synthy hymn", lyrics=None, webhook_url="http://web:8000/webhook"
        )
    assert handles.task_id == "t1"
    assert handles.conversion_id_1 == "c1"
    assert handles.conversion_id_2 == "c2"
    assert handles.eta == 60
    assert handles.credit_estimate == 1.0


async def test_create_raises_rate_limited_on_429() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(429, text="high demand"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRateLimited):
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )


async def test_create_raises_rejected_on_terminal_4xx() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(422, text="bad prompt"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRejected) as exc_info:
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )
    assert exc_info.value.status_code == 422


async def test_create_raises_transient_error_on_5xx() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )


async def test_create_raises_transient_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )


async def test_create_raises_transient_error_on_malformed_json_body() -> None:
    """Quality report HIGH finding: a malformed 200 body (invalid JSON) must be a
    retriable transient condition, not an uncaught `JSONDecodeError` that escapes
    `dispatch_claimed_job` and strands the job / leaks the semaphore slot."""
    handler = httpx.MockTransport(lambda r: httpx.Response(200, text="not json"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )


async def test_create_raises_transient_error_on_200_missing_a_required_field() -> None:
    """A 200 body that's valid JSON but missing an expected field (e.g. `task_id`)
    must also be treated as transient/retriable, not an uncaught `KeyError`."""
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={"conversion_id_1": "c1", "conversion_id_2": "c2", "eta": 60, "credit_estimate": 1.0},
        )
    )
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.create(
                prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
            )


async def test_create_sends_empty_string_lyrics_when_none() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "task_id": "t1", "conversion_id_1": "c1", "conversion_id_2": "c2",
                "eta": 60, "credit_estimate": 1.0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        await client.create(
            prompt="p", lyrics=None, webhook_url="http://web:8000/webhook"
        )
    assert captured["lyrics"] == ""
    assert captured["prompt"] == "p"
    assert captured["webhook_url"] == "http://web:8000/webhook"


# ── Against the REAL simulator app (data-contract audit, issue #12 phase 4) ───────


async def test_create_against_real_simulator_returns_handles_matching_the_contract() -> None:
    """Drives the real `POST /api/public/v1/MusicAI` (simulator/routes/create.py),
    not a hand-crafted MockTransport dict -- proves `HttpGenerationClient.create`'s
    parsing (`data["task_id"]` etc., `jobs/generation_client.py`) actually matches
    the real `CreateResponse` the simulator emits (`simulator/schemas.py`), including
    types (`eta` int, `credit_estimate` float)."""
    rig = make_rig()
    async with rig.client:
        client = HttpGenerationClient(_settings(), rig.client)

        handles = await client.create(
            prompt=str(DEFAULT_BODY["prompt"]),
            lyrics=None,
            webhook_url=str(DEFAULT_BODY["webhook_url"]),
        )

    assert handles.task_id
    assert handles.conversion_id_1
    assert handles.conversion_id_2
    assert handles.conversion_id_1 != handles.conversion_id_2
    assert isinstance(handles.eta, int)
    assert isinstance(handles.credit_estimate, float)


async def test_create_against_real_simulator_raises_rate_limited_on_real_429() -> None:
    """The simulator's `X-Sim-Fault: rate-limit-429` (`simulator/faults.py`) is the
    real 429 backstop (PRD #18) this client must map to `GenerationRateLimited` --
    not a hand-crafted `httpx.Response(429, ...)` standing in for it."""
    rig = make_rig()
    async with rig.client:
        # A fresh client bound to the same real simulator app, with the fault header
        # set at the client level so it rides along on every request `create()`
        # makes -- `HttpGenerationClient.create`'s signature has no headers param
        # (nor should it; fault injection is a test-only concern of the simulator).
        faulty_client = make_sim_client(rig.app)
        faulty_client.headers[FAULT_HEADER] = Fault.RATE_LIMIT_429.value
        async with faulty_client:
            client = HttpGenerationClient(_settings(), faulty_client)
            with pytest.raises(GenerationRateLimited):
                await client.create(
                    prompt=str(DEFAULT_BODY["prompt"]),
                    lyrics=None,
                    webhook_url=str(DEFAULT_BODY["webhook_url"]),
                )


# ── `get_audio_url_by_id` (issue #13: by-handle URL refresh, `GET {base}/byId`) ───
#
# The webhook's `conversion_path` is only a *hint*; when it's expired or the download
# fails, the ingest path refreshes it via the generation API's by-handle lookup
# (PRD #6 "Object storage & Generation pipeline"). Mirrors `create`'s two-tier test
# structure above: hand-crafted `MockTransport` responses first, then the real
# simulator (`simulator/routes/by_id.py` + `simulator/schemas.py::ByIdResponse`) so
# fixture drift on either side can't hide a break.


async def test_get_audio_url_by_id_returns_fresh_url_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/byId"
        assert request.url.params["task_id"] == "t1"
        return httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "COMPLETED",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
                "audio_url": "http://musicgpt.test/audio/fresh-token",
                "conversion_duration": 123.4,
                "title": "A Song",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        url = await client.get_audio_url_by_id("t1")

    assert url == "http://musicgpt.test/audio/fresh-token"


async def test_get_audio_url_by_id_raises_rejected_on_404() -> None:
    """Unknown `task_id` mirrors `simulator/routes/by_id.py`'s 404 -- not retriable."""
    handler = httpx.MockTransport(lambda r: httpx.Response(404, text="unknown task_id"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRejected) as exc_info:
            await client.get_audio_url_by_id("unknown")
    assert exc_info.value.status_code == 404


async def test_get_audio_url_by_id_raises_rate_limited_on_429() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(429, text="high demand"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRateLimited):
            await client.get_audio_url_by_id("t1")


async def test_get_audio_url_by_id_raises_transient_error_on_5xx() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_audio_url_by_id("t1")


async def test_get_audio_url_by_id_raises_transient_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_audio_url_by_id("t1")


async def test_get_audio_url_by_id_raises_transient_error_on_malformed_json_body() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, text="not json"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_audio_url_by_id("t1")


async def test_get_audio_url_by_id_raises_transient_error_on_200_missing_audio_url() -> None:
    """A 200 body missing the expected `audio_url` field (malformed API response, not
    the legitimate "not completed yet" case which the real simulator never returns
    from a freshly-minted refresh) must be treated as transient/retriable."""
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "COMPLETED",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
            },
        )
    )
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_audio_url_by_id("t1")


# ── Against the REAL simulator (data-contract audit) ──────────────────────────────


async def test_get_audio_url_by_id_against_real_simulator_returns_a_fresh_url() -> None:
    """Drives the real `GET /byId` (simulator/routes/by_id.py) end to end -- proves
    `HttpGenerationClient.get_audio_url_by_id`'s parsing matches the real
    `ByIdResponse` shape (simulator/schemas.py), not a hand-crafted MockTransport
    dict."""
    rig = make_rig()
    async with rig.client:
        client = HttpGenerationClient(_settings(), rig.client)
        handles = await client.create(
            prompt=str(DEFAULT_BODY["prompt"]),
            lyrics=None,
            webhook_url=str(DEFAULT_BODY["webhook_url"]),
        )
        await wait_for_pending_webhooks(rig.app)

        url = await client.get_audio_url_by_id(handles.task_id)

    assert url
    assert url.startswith("http")


async def test_get_audio_url_by_id_against_real_sim_rejects_unknown_task() -> None:
    rig = make_rig()
    async with rig.client:
        client = HttpGenerationClient(_settings(), rig.client)
        with pytest.raises(GenerationRejected) as exc_info:
            await client.get_audio_url_by_id("does-not-exist")
    assert exc_info.value.status_code == 404


# ── get_status_by_id (issue #16: status-branching by-id lookup for the watchdog) ──
#
# Unlike `get_audio_url_by_id` (raises unless the task is COMPLETED), the watchdog's
# `jobs/watchdog.py::sweep_waiting_overdue` needs the raw STATUS to branch: COMPLETED
# recovers a lost webhook (no re-charge), ERROR/FAILED are terminal, IN_QUEUE is left
# waiting. Mirrors `get_audio_url_by_id`'s two-tier test structure (hand-crafted
# `MockTransport` first, then the real simulator). Currently RED at runtime (not
# collection): `HttpGenerationClient.get_status_by_id` raises `NotImplementedError`
# (issue #16 phase 3) -- see that method's docstring in `jobs/generation_client.py`.


async def test_get_status_by_id_returns_completed_with_audio_url_duration_and_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/byId"
        assert request.url.params["task_id"] == "t1"
        return httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "COMPLETED",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
                "audio_url": "http://musicgpt.test/audio/fresh-token",
                "conversion_duration": 123.4,
                "title": "A Song",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        status = await client.get_status_by_id("t1")

    assert status.status == GENERATION_STATUS_COMPLETED
    assert status.audio_url == "http://musicgpt.test/audio/fresh-token"
    assert status.duration == 123.4
    assert status.title == "A Song"


async def test_get_status_by_id_returns_in_queue_with_no_audio_url() -> None:
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "IN_QUEUE",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
            },
        )
    )
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        status = await client.get_status_by_id("t1")

    assert status.status == GENERATION_STATUS_IN_QUEUE
    assert status.audio_url is None


async def test_get_status_by_id_returns_error_status() -> None:
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "ERROR",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
            },
        )
    )
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        status = await client.get_status_by_id("t1")

    assert status.status == GENERATION_STATUS_ERROR


async def test_get_status_by_id_returns_failed_status() -> None:
    handler = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "task_id": "t1",
                "status": "FAILED",
                "conversion_id_1": "c1",
                "conversion_id_2": "c2",
            },
        )
    )
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        status = await client.get_status_by_id("t1")

    assert status.status == GENERATION_STATUS_FAILED


async def test_get_status_by_id_raises_rejected_on_404() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(404, text="unknown task_id"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRejected) as exc_info:
            await client.get_status_by_id("unknown")
    assert exc_info.value.status_code == 404


async def test_get_status_by_id_raises_rate_limited_on_429() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(429, text="high demand"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationRateLimited):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_raises_transient_error_on_5xx() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_raises_transient_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_raises_transient_error_on_malformed_json_body() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, text="not json"))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_raises_transient_error_on_a_non_dict_json_body() -> None:
    """Valid JSON that isn't an object (e.g. a bare array) is a distinct malformed-200
    branch from a JSON parse failure -- `data.get(...)` would raise `AttributeError`
    on a list, so this must be caught and mapped to the same typed exception rather
    than crashing the watchdog loop."""
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json=["not", "an", "object"]))
    async with httpx.AsyncClient(transport=handler) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_raises_transient_error_on_a_network_connection_error() -> None:
    """A non-timeout `httpx.RequestError` (e.g. connection refused) must map the same
    way a timeout does -- both are retriable, neither is a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = HttpGenerationClient(_settings(), http_client)
        with pytest.raises(GenerationTransientError):
            await client.get_status_by_id("t1")


async def test_get_status_by_id_against_real_simulator_returns_completed() -> None:
    """Drives the real `GET /byId` end to end (mirrors
    `test_get_audio_url_by_id_against_real_simulator_returns_a_fresh_url`) -- proves
    `get_status_by_id`'s parsing matches the real `ByIdResponse` shape once
    implemented."""
    rig = make_rig()
    async with rig.client:
        client = HttpGenerationClient(_settings(), rig.client)
        handles = await client.create(
            prompt=str(DEFAULT_BODY["prompt"]),
            lyrics=None,
            webhook_url=str(DEFAULT_BODY["webhook_url"]),
        )
        await wait_for_pending_webhooks(rig.app)

        status = await client.get_status_by_id(handles.task_id)

    assert status.status == GENERATION_STATUS_COMPLETED
    assert status.audio_url
    assert status.audio_url.startswith("http")
