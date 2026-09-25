"""Redis semaphore: global + per-user in-flight generation slot cap (issue #12, #3).

Two atomic counters gate dispatch's call to the generation API: ``global_generation_
concurrency`` in-flight jobs system-wide, ``per_user_concurrent_jobs`` in-flight jobs
per identity. Acquiring both must be atomic and paired — if the per-user leg can't be
acquired after the global leg was, the global slot is released rather than leaked
(spec: "acquire global slot AND per-user slot together; if either would exceed, release
the other and fail" — the criterion #3 "never exceeds the global cap" guarantee depends
on this; a leaked global slot would silently shrink capacity over time).

Split into a pure cap-decision layer (``Semaphore``/``RedisSemaphore``) and an atomic
counter-primitive backend (``SemaphoreBackend``/``RedisSemaphoreBackend``, Lua-script
backed for true cross-process atomicity — plain Redis commands alone can't do "check
global cap AND check user cap AND increment both" as one step), mirroring this repo's
convention of separating decision logic from I/O wiring (see ``radio/coordinator.py``
vs ``worker/radio_coordinator.py``). ``tests/test_semaphore.py`` exercises
``RedisSemaphore`` against a file-local in-memory fake backend on the fast unit path;
the real Lua scripts are exercised only where a live Redis is available.

Redis is derived/ephemeral (see ``redis_client.py``): ``reconcile_from_active_count``
resets the global counter from Postgres truth (the source of record) on the 429 path,
so a lost/stale slot never permanently shrinks capacity.
"""

from __future__ import annotations

from typing import Protocol

from redis.asyncio import Redis

from songforge.config import Settings
from songforge.logging_setup import get_logger
from songforge.metrics import semaphore_acquire_denied_total

log = get_logger(__name__)

# KEYS[1] = the counter key. ARGV[1] = the cap. Returns 1 (incremented) or 0 (at cap).
# EVAL-based rather than GET-then-INCR because Redis runs a script single-threaded to
# completion -- a bare GET + compare + INCR has a race window between the two commands
# that two concurrent worker processes could both slip through, exceeding the cap.
_TRY_INCREMENT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
  return 0
end
redis.call('INCR', KEYS[1])
return 1
"""

# KEYS[1] = the counter key. Floors at 0 so a double-release (or a release with no
# matching acquire) can never take a counter negative.
_DECREMENT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
  redis.call('DECR', KEYS[1])
end
return 1
"""


def global_key(settings: Settings) -> str:
    """Redis key holding the global in-flight generation counter."""
    return settings.semaphore_global_key


def user_key(settings: Settings, user_id: str) -> str:
    """Redis key holding one user's in-flight generation counter."""
    return f"{settings.semaphore_user_key_prefix}{user_id}"


class SemaphoreBackend(Protocol):
    """Atomic counter primitives the cap decision (``RedisSemaphore``) is built on.

    Production (``RedisSemaphoreBackend``) implements these via Redis Lua scripts for
    true cross-process atomicity. Tests substitute a simple in-process fake (see
    ``tests/test_semaphore.py``) — safe because a Python coroutine with no internal
    ``await`` never yields to another task mid-body, so the fake's sequential dict
    mutations are already atomic with respect to concurrent ``asyncio.gather`` callers.
    """

    async def try_increment(self, key: str, cap: int) -> bool:
        """Atomically increment ``key`` iff doing so would not exceed ``cap``.

        Returns whether the increment was applied.
        """
        ...

    async def decrement(self, key: str) -> None:
        """Atomically decrement ``key``, floored at 0 (a release must never go
        negative, e.g. on a double-release or a release with no matching acquire)."""
        ...

    async def set_value(self, key: str, value: int) -> None:
        """Atomically set ``key`` to ``value`` (used by ``reconcile_from_active_count``)."""
        ...

    async def scan_keys(self, match: str) -> list[str]:
        """Enumerate every key matching ``match`` (a ``<prefix>*`` glob) -- the
        watchdog reconcile backstop's (issue #16) way of discovering which per-user
        semaphore keys currently exist, without needing a separate index of every
        user id that has ever acquired a slot."""
        ...


class RedisSemaphoreBackend:
    """Real backend: Redis-native atomic ops via Lua scripts (cross-process atomicity).

    ``EVAL``-based rather than plain ``INCR``/``DECR`` because "check cap, then
    increment" must be one atomic step across concurrent Redis clients (multiple worker
    processes) — a bare ``GET`` + compare + ``INCR`` has a race window between the two
    commands that a Lua script (which Redis runs single-threaded, to completion) closes.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def try_increment(self, key: str, cap: int) -> bool:
        result = await self._redis.eval(_TRY_INCREMENT_LUA, 1, key, cap)
        return bool(result)

    async def decrement(self, key: str) -> None:
        await self._redis.eval(_DECREMENT_LUA, 1, key)

    async def set_value(self, key: str, value: int) -> None:
        await self._redis.set(key, value)

    async def scan_keys(self, match: str) -> list[str]:
        # `SCAN`-based (via `scan_iter`), not `KEYS` -- `KEYS` blocks the single
        # Redis event loop for the duration of a full-keyspace scan (a real
        # production hazard at any nontrivial key count), where `SCAN` walks the
        # keyspace in small cursor-driven steps.
        return [key async for key in self._redis.scan_iter(match=match)]


class Semaphore(Protocol):
    """Cap-decision interface ``songforge.jobs.dispatch`` depends on."""

    async def acquire(self, user_id: str) -> bool:
        """Acquire one global + one per-user slot together, or neither. Must be called
        (and must succeed) BEFORE the generation API is called (criterion #3)."""
        ...

    async def release(self, user_id: str) -> None:
        """Release one global + one per-user slot (floored at 0 each)."""
        ...

    async def reconcile_from_active_count(self, count: int) -> None:
        """Reset the global counter to ``count`` (a fresh count of active-state job
        rows) — the 429 path's way of correcting semaphore drift from Postgres truth."""
        ...

    async def scan_user_ids(self) -> list[str]:
        """List every user id with a per-user semaphore key currently in Redis (the
        watchdog reconcile backstop's discovery step, issue #16) -- a key exists for
        any user who has ever acquired a slot, since ``release``/``decrement`` floor
        at 0 rather than deleting the key."""
        ...

    async def reconcile_user_from_active_count(self, user_id: str, count: int) -> None:
        """Reset ``user_id``'s per-user counter to ``count`` (a fresh count of that
        user's active-state job rows) -- the per-user counterpart to
        ``reconcile_from_active_count``, called once per id ``scan_user_ids`` finds."""
        ...


class RedisSemaphore:
    """Pairs a global + per-user atomic increment via ``SemaphoreBackend`` (criterion #3)."""

    def __init__(self, backend: SemaphoreBackend, settings: Settings) -> None:
        self._backend = backend
        self._settings = settings

    async def acquire(self, user_id: str) -> bool:
        g_key = global_key(self._settings)
        u_key = user_key(self._settings, user_id)

        acquired_global = await self._backend.try_increment(
            g_key, self._settings.global_generation_concurrency
        )
        if not acquired_global:
            semaphore_acquire_denied_total.inc()
            log.info("semaphore_acquire_denied", user_id=user_id, cap="global")
            return False

        acquired_user = await self._backend.try_increment(
            u_key, self._settings.per_user_concurrent_jobs
        )
        if not acquired_user:
            # Roll back the global leg rather than leak it -- criterion #3's "never
            # exceeds the global cap" would otherwise degrade over time.
            await self._backend.decrement(g_key)
            semaphore_acquire_denied_total.inc()
            log.info("semaphore_acquire_denied", user_id=user_id, cap="per_user")
            return False

        return True

    async def release(self, user_id: str) -> None:
        await self._backend.decrement(global_key(self._settings))
        await self._backend.decrement(user_key(self._settings, user_id))
        log.info("semaphore_released", user_id=user_id)

    async def reconcile_from_active_count(self, count: int) -> None:
        await self._backend.set_value(global_key(self._settings), count)
        log.info("semaphore_reconciled", active_count=count)

    async def scan_user_ids(self) -> list[str]:
        prefix = self._settings.semaphore_user_key_prefix
        keys = await self._backend.scan_keys(f"{prefix}*")
        return [key[len(prefix) :] for key in keys]

    async def reconcile_user_from_active_count(self, user_id: str, count: int) -> None:
        await self._backend.set_value(user_key(self._settings, user_id), count)
        log.info("semaphore_user_reconciled", user_id=user_id, active_count=count)
