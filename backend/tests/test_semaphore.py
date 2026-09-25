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

    async def scan_keys(self, match: str) -> list[str]:
        # `match` is always a `<prefix>*` glob in this codebase (see
        # `RedisSemaphore.scan_user_ids`) -- a plain prefix check stands in for the
        # real backend's `SCAN`/`scan_iter`, which is exercised only against a live
        # Redis (see the module docstring's fast-unit-path/live-Redis split).
        assert match.endswith("*"), f"unsupported fake scan_keys match: {match!r}"
        prefix = match[:-1]
        return [key for key in self._counts if key.startswith(prefix)]

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


async def test_double_release_after_a_single_acquire_floors_at_zero_not_negative() -> None:
    """A second release with no matching second acquire (e.g. a dispatch bug calling
    release twice on one job) must never take a counter negative -- a negative counter
    would silently raise effective capacity above the configured cap."""
    settings = _settings(global_generation_concurrency=5, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")

    await sem.release("user-a")
    await sem.release("user-a")  # double release

    assert backend.get(global_key(settings)) == 0
    assert backend.get(user_key(settings, "user-a")) == 0


async def test_reconcile_sets_the_global_counter_from_postgres_truth() -> None:
    settings = _settings(global_generation_concurrency=5, per_user_concurrent_jobs=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")  # global counter now 1, out of sync with "truth"

    await sem.reconcile_from_active_count(3)

    assert backend.get(global_key(settings)) == 3


async def test_reconcile_corrects_the_counter_both_upward_and_downward() -> None:
    """The 429 path's reconcile must be able to correct drift in either direction --
    Postgres truth (a fresh active-row count) might be higher OR lower than the Redis
    counter, depending on what drifted (e.g. a crashed dispatcher that never released,
    vs. a released slot that never got reflected)."""
    settings = _settings(global_generation_concurrency=10, per_user_concurrent_jobs=10)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")  # global counter now 1

    await sem.reconcile_from_active_count(6)  # correct upward
    assert backend.get(global_key(settings)) == 6

    await sem.reconcile_from_active_count(2)  # correct downward
    assert backend.get(global_key(settings)) == 2


async def test_per_user_acquire_never_lets_total_in_flight_exceed_the_global_cap_across_users() -> (
    None
):
    """Issue #15, criterion #2 (first half), confirmatory: even with many DIFFERENT
    users each acquiring up to their OWN per-user cap, the TOTAL in-flight across all of
    them can never exceed ``global_generation_concurrency`` -- this is already
    guaranteed by ``acquire``'s global-then-per-user pairing (see the module docstring
    and ``test_failed_per_user_acquire_does_not_leak_a_global_slot`` above). This test
    documents/locks that existing guarantee for issue #15 rather than driving new
    behavior, so unlike the rest of this issue's tests it is expected to be GREEN
    immediately."""
    settings = _settings(global_generation_concurrency=3, per_user_concurrent_jobs=2)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    users = [f"user-{i}" for i in range(5)]

    # Each of 5 distinct users tries to acquire up to their own per-user cap (2),
    # concurrently -- 10 total attempts against a global cap of 3.
    results = await asyncio.gather(*(sem.acquire(user) for user in users for _ in range(2)))

    granted = sum(1 for ok in results if ok)
    assert granted == 3  # the global cap, not the sum of per-user caps (10)
    assert backend.get(global_key(settings)) == 3
    # No single user was granted more than their own per-user cap either.
    for user in users:
        assert backend.get(user_key(settings, user)) <= 2


async def test_concurrent_acquires_never_exceed_the_global_cap() -> None:
    settings = _settings(global_generation_concurrency=3, per_user_concurrent_jobs=20)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    results = await asyncio.gather(*(sem.acquire(f"user-{i}") for i in range(10)))

    assert sum(1 for granted in results if granted) == 3
    assert backend.get(global_key(settings)) == 3


# ── Watchdog reconcile backstop, per-user leg (issue #16 slot-leak fix) ────────────
#
# The global reconcile (`reconcile_from_active_count`) was already covered above. The
# per-user leg needs a way to discover WHICH user keys currently exist in Redis (a
# leaked/never-released key from a crashed process) before it can snap each one back
# to its owner's fresh active-row count -- `scan_user_ids` (backed by
# `SemaphoreBackend.scan_keys`) is that discovery step, and
# `reconcile_user_from_active_count` is the per-key set-from-truth step
# `worker/watchdog.py`'s reconcile loop calls once per discovered user id.


async def test_scan_user_ids_returns_user_ids_for_existing_per_user_keys() -> None:
    settings = _settings(global_generation_concurrency=5)
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")
    await sem.acquire("user-b")

    user_ids = await sem.scan_user_ids()

    assert sorted(user_ids) == ["user-a", "user-b"]


async def test_scan_user_ids_returns_empty_list_when_no_per_user_keys_exist() -> None:
    settings = _settings()
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)

    assert await sem.scan_user_ids() == []


async def test_scan_user_ids_never_returns_the_global_key_itself() -> None:
    """The global key (`sem:gen:global`) shares no prefix with a per-user key
    (`sem:gen:user:{id}`) -- confirms `scan_user_ids` scopes its scan to the per-user
    prefix, not a bare `sem:gen:*` that would also match the global counter."""
    settings = _settings()
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")  # touches both the global key and a per-user key

    user_ids = await sem.scan_user_ids()

    assert user_ids == ["user-a"]


async def test_reconcile_user_from_active_count_sets_the_per_user_counter() -> None:
    settings = _settings()
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")
    await sem.acquire("user-a")  # per-user counter now 2, out of sync with "truth"

    await sem.reconcile_user_from_active_count("user-a", 1)

    assert backend.get(user_key(settings, "user-a")) == 1


async def test_reconcile_user_from_active_count_can_snap_a_leaked_key_down_to_zero() -> None:
    """The realistic leak scenario: a crashed process acquired a slot and never
    released it (no active rows left for that user), so reconcile must be able to
    snap the counter all the way back to 0, not just correct within a non-zero range."""
    settings = _settings()
    backend = _InMemorySemaphoreBackend()
    sem = RedisSemaphore(backend, settings)
    await sem.acquire("user-a")

    await sem.reconcile_user_from_active_count("user-a", 0)

    assert backend.get(user_key(settings, "user-a")) == 0
