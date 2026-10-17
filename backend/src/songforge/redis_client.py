"""Async Redis client — derived/ephemeral state (semaphore, counters, pub/sub, caches).

Redis is always rebuildable from Postgres; on divergence Postgres wins (spec datastore
ownership). This scaffold exposes the connection and a health ping; the semaphore and
rate-limit counters are added by later tickets.
"""

from __future__ import annotations

import functools

from redis.asyncio import Redis

from songforge.config import get_settings


@functools.lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def check_redis() -> bool:
    """Return True if Redis answers PING; used by the readiness probe."""
    client = get_redis()
    return bool(await client.ping())
