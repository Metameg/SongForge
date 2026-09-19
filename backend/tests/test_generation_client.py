"""Unit tests for the generation API client (issue #12, criterion #4). Exercises
`jobs/generation_client.HttpGenerationClient.create` against `httpx.MockTransport` --
no real network, matching the repo's "all tests mock external services" convention.
Field shape mirrors the issue #11 simulator's contract (`simulator/schemas.py`).
"""

from __future__ import annotations

import httpx
import pytest

from songforge.config import Settings
from songforge.jobs.generation_client import (
    GenerationRateLimited,
    GenerationRejected,
    GenerationTransientError,
    HttpGenerationClient,
)


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
