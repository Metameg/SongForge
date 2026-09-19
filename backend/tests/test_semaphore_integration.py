"""Real-Redis atomicity for the generation semaphore (issue #12, criterion #3).

`tests/test_semaphore.py` proves the cap-decision logic (`RedisSemaphore`) against an
in-memory fake backend. This file proves the *production* backend
(`RedisSemaphoreBackend`, Lua `EVAL`) is genuinely atomic across concurrent Redis
clients -- a plain `GET`-then-`INCR` pair could let two callers both read under-cap and
both increment, exceeding the global ceiling; a real Redis server is required to prove
that can't happen (a Python-level fake can't demonstrate cross-process atomicity).

Connects to `TEST_REDIS_URL` if set, else the docker-compose dev default
(`redis://127.0.0.1:56379/0`). Skips the whole module (not fails) if unreachable, same
convention as `tests/test_jobs_queue_integration.py`.
"""

from __future__ import annotations

import asyncio
import os
import socket
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.integration

_DEFAULT_REDIS_URL = "redis://127.0.0.1:56379/0"
_TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", _DEFAULT_REDIS_URL)
_CONNECT_TIMEOUT_SECONDS = 1.5


def _redis_reachable() -> bool:
    parts = urlsplit(_TEST_REDIS_URL)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 6379
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_redis() -> None:
    if not _redis_reachable():
        pytest.skip(f"Redis unreachable at {_TEST_REDIS_URL!r}")


@pytest.fixture()
async def redis():  # type: ignore[no-untyped-def]
    from redis.asyncio import Redis

    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    # Isolate this file's keys from anything else that might use the same Redis.
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def test_concurrent_acquires_never_exceed_the_global_cap(redis) -> None:  # type: ignore[no-untyped-def]
    from songforge.config import Settings
    from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, global_key

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": _TEST_REDIS_URL,
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "GLOBAL_GENERATION_CONCURRENCY": "3",
            "PER_USER_CONCURRENT_JOBS": "10",
        }
    )
    sem = RedisSemaphore(RedisSemaphoreBackend(redis), settings)

    results = await asyncio.gather(*(sem.acquire(f"user-{i}") for i in range(20)))

    assert sum(1 for granted in results if granted) == 3
    assert await redis.get(global_key(settings)) == "3"


async def test_release_then_reconcile_against_real_redis(redis) -> None:  # type: ignore[no-untyped-def]
    from songforge.config import Settings
    from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, global_key

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": _TEST_REDIS_URL,
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "GLOBAL_GENERATION_CONCURRENCY": "5",
            "PER_USER_CONCURRENT_JOBS": "5",
        }
    )
    sem = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    await sem.acquire("user-a")
    await sem.acquire("user-b")

    await sem.release("user-a")
    assert await redis.get(global_key(settings)) == "1"

    await sem.reconcile_from_active_count(0)
    assert await redis.get(global_key(settings)) == "0"


async def test_double_release_floors_at_zero_against_real_redis(redis) -> None:  # type: ignore[no-untyped-def]
    """`tests/test_semaphore.py` proves this against the in-memory fake; this proves
    the real Lua `_DECREMENT_LUA` script itself floors at zero -- a plain `DECR` would
    happily go negative, silently raising effective capacity above the cap."""
    from songforge.config import Settings
    from songforge.jobs.semaphore import (
        RedisSemaphore,
        RedisSemaphoreBackend,
        global_key,
        user_key,
    )

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": _TEST_REDIS_URL,
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "GLOBAL_GENERATION_CONCURRENCY": "5",
            "PER_USER_CONCURRENT_JOBS": "5",
        }
    )
    sem = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    await sem.acquire("user-a")

    await sem.release("user-a")
    await sem.release("user-a")  # double release

    assert await redis.get(global_key(settings)) == "0"
    assert await redis.get(user_key(settings, "user-a")) == "0"


async def test_reconcile_corrects_upward_and_downward_against_real_redis(redis) -> None:  # type: ignore[no-untyped-def]
    from songforge.config import Settings
    from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, global_key

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": _TEST_REDIS_URL,
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "GLOBAL_GENERATION_CONCURRENCY": "10",
            "PER_USER_CONCURRENT_JOBS": "10",
        }
    )
    sem = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    await sem.acquire("user-a")  # counter now 1

    await sem.reconcile_from_active_count(7)
    assert await redis.get(global_key(settings)) == "7"

    await sem.reconcile_from_active_count(3)
    assert await redis.get(global_key(settings)) == "3"


async def test_failed_per_user_acquire_does_not_leak_a_global_slot_against_real_redis(
    redis,  # type: ignore[no-untyped-def]
) -> None:
    """`tests/test_semaphore.py` proves the pairing/rollback logic against the fake;
    this proves the real Lua-scripted backend genuinely leaves the global counter
    untouched (not just "eventually consistent") when the paired per-user leg is
    denied."""
    from songforge.config import Settings
    from songforge.jobs.semaphore import RedisSemaphore, RedisSemaphoreBackend, global_key

    settings = Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": _TEST_REDIS_URL,
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
            "GLOBAL_GENERATION_CONCURRENCY": "10",
            "PER_USER_CONCURRENT_JOBS": "1",
        }
    )
    sem = RedisSemaphore(RedisSemaphoreBackend(redis), settings)
    assert await sem.acquire("user-a") is True  # consumes user-a's only slot
    global_after_first = await redis.get(global_key(settings))

    blocked = await sem.acquire("user-a")  # per-user cap (1) denies this one

    assert blocked is False
    assert await redis.get(global_key(settings)) == global_after_first  # not leaked
