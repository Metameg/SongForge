"""Unit tests for the Redis semaphore cap logic (issue #12, criterion #3).

Exercises ``RedisSemaphore`` against a file-local in-memory fake ``SemaphoreBackend``
(no live Redis — `fakeredis` isn't installed in this env; see
``songforge/jobs/semaphore.py``'s module docstring for why a fake with no internal
``await`` is already atomic under ``asyncio.gather``, matching this repo's
``_FakeRedis``/``_StubHistory`` file-local-stub convention, e.g.
``tests/test_now_playing.py``, ``tests/test_radio_coordinator.py``). The production
Lua-script atomicity itself is exercised only where a live Redis is available.

Production change that turns these green: implementing ``RedisSemaphore.acquire`` /
``.release`` / ``.reconcile_from_active_count`` in ``songforge/jobs/semaphore.py``.
"""

from __future__ import annotations

import asyncio

from songforge.config import Settings
from songforge.jobs.semaphore import RedisSemaphore, global_key, user_key


class _InMemorySemaphoreBackend:
    """File-local fake ``SemaphoreBackend``: plain dict counters, no real Redis."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    async def try_increment(self, key: str, cap: int) -> bool:
        current = self._counts.get(key, 0)
        if current >= cap:
            return False
        self._counts[key] = current + 1
        return True

    async def decrement(self, key: str) -> None:
        self._counts[key] = max(self._counts.get(key, 0) - 1, 0)

    async def set_value(self, key: str, value: int) -> None:
        self._counts[key] = value

    def get(self, key: str) -> int:
        return self._counts.get(key, 0)


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


async def test_global_cap_blocks_acquires_past_the_limit() -> None:
    settings = _settings(global_generation_concurrency=2, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    assert await sem.acquire("user-a") is True
    assert await sem.acquire("user-b") is True
    assert await sem.acquire("user-c") is False  # global cap (2) reached
    assert backend.get(global_key(settings)) == 2


async def test_per_user_cap_blocks_independently_of_other_users() -> None:
    settings = _settings(global_generation_concurrency=10, per_user_concurrent_jobs=2)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    assert await sem.acquire("user-a") is True
    assert await sem.acquire("user-a") is True
    assert await sem.acquire("user-a") is False  # per-user cap (2) reached
    assert await sem.acquire("user-b") is True  # a different user is unaffected


async def test_failed_per_user_acquire_does_not_leak_a_global_slot() -> None:
    """Atomicity: global+user must be acquired as a pair. If the user leg fails, the
    global increment must be rolled back, not left dangling — otherwise criterion #3
    ("never exceeds the global cap") degrades over time as slots leak."""
    settings = _settings(global_generation_concurrency=10, per_user_concurrent_jobs=1)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")  # consumes user-a's only slot
    global_before = backend.get(global_key(settings))

    blocked = await sem.acquire("user-a")  # per-user cap blocks this one

    assert blocked is False
    assert backend.get(global_key(settings)) == global_before  # not leaked


async def test_release_decrements_both_counters() -> None:
    settings = _settings(global_generation_concurrency=5, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")

    await sem.release("user-a")

    assert backend.get(global_key(settings)) == 0
    assert backend.get(user_key(settings, "user-a")) == 0


async def test_release_floors_at_zero() -> None:
    settings = _settings(global_generation_concurrency=5, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    await sem.release("user-with-no-prior-acquire")

    assert backend.get(global_key(settings)) == 0
    assert backend.get(user_key(settings, "user-with-no-prior-acquire")) == 0


async def test_reconcile_sets_the_global_counter_from_postgres_truth() -> None:
    settings = _settings(global_generation_concurrency=5, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")  # global counter now 1, out of sync with "truth"

    await sem.reconcile_from_active_count(3)

    assert backend.get(global_key(settings)) == 3


async def test_concurrent_acquires_never_exceed_the_global_cap() -> None:
    settings = _settings(global_generation_concurrency=3, per_user_concurrent_jobs=20)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    results = await asyncio.gather(*(sem.acquire(f"user-{i}") for i in range(10)))

    assert sum(1 for granted in results if granted) == 3
    assert backend.get(global_key(settings)) == 3
