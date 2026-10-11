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
corrects the global counter toward Postgres truth (the source of record) on the 429
path and from the watchdog's periodic backstop, so a lost/stale slot never permanently
shrinks capacity.

Reconcile is DECREASE-ONLY (``SemaphoreBackend.decrease_to``), never a blind set. A
fresh Postgres active-row count can momentarily read LOWER than the real in-flight
count — ``jobs/dispatch.py``'s own claim step commits a job's ``SUBMITTING`` row to
Postgres *before* that same job's ``acquire()`` runs, so there is a real window where
Postgres already shows the job active while Redis has not yet recorded its slot; a
reconcile landing in that window would otherwise raise the Redis counter to credit a
slot the job's own upcoming ``acquire()`` hasn't taken yet — which then DENIES that
real acquire (``current >= cap``) and leaves a phantom count nobody will ever release,
freezing dispatch until the next reconcile tick happens to see 0 active jobs again.
Restricting reconcile to only ever LOWER the counter keeps its one legitimate purpose
(un-leaking a slot whose release never fired, e.g. a crashed worker) while removing the
only direction that can ever push the counter above a job that hasn't acquired yet.
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

# KEYS[1] = the counter key. ARGV[1] = the candidate value (a fresh Postgres active-row
# count). Writes only when that would be a DECREASE (candidate < current, treating a
# missing key as 0) -- see the module docstring for why reconcile must never raise this
# counter. Returns 1 if written, 0 if left unchanged. EVAL-based for the same
# check-then-write atomicity reason as `_TRY_INCREMENT_LUA`.
_DECREASE_TO_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local candidate = tonumber(ARGV[1])
if candidate < current then
  redis.call('SET', KEYS[1], candidate)
  return 1
end
return 0
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

    async def decrease_to(self, key: str, candidate: int) -> bool:
        """Atomically set ``key`` to ``candidate`` IFF that would be a decrease
        (``candidate`` < the current value, treating a missing key as 0) -- used by
        ``reconcile_from_active_count``/``reconcile_user_from_active_count``. Never
        raises the counter (see the module docstring for why). Returns whether the
        write happened."""
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

    async def decrease_to(self, key: str, candidate: int) -> bool:
        result = await self._redis.eval(_DECREASE_TO_LUA, 1, key, candidate)
        return bool(result)

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
        """Correct the global counter toward ``count`` (a fresh count of active-state
        job rows) — the 429 path's and the watchdog's way of un-leaking a slot whose
        release never fired. DECREASE-ONLY: never raises the counter above its current
        value (see the module docstring for why a momentarily-higher Postgres count
        must not be blindly trusted upward)."""
        ...

    async def scan_user_ids(self) -> list[str]:
        """List every user id with a per-user semaphore key currently in Redis (the
        watchdog reconcile backstop's discovery step, issue #16) -- a key exists for
        any user who has ever acquired a slot, since ``release``/``decrement`` floor
        at 0 rather than deleting the key."""
        ...

    async def reconcile_user_from_active_count(self, user_id: str, count: int) -> None:
        """Correct ``user_id``'s per-user counter toward ``count`` (a fresh count of
        that user's active-state job rows) -- the per-user, DECREASE-ONLY counterpart
        to ``reconcile_from_active_count``, called once per id ``scan_user_ids``
        finds."""
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
        changed = await self._backend.decrease_to(global_key(self._settings), count)
        log.info("semaphore_reconciled", active_count=count, changed=changed)

    async def scan_user_ids(self) -> list[str]:
        prefix = self._settings.semaphore_user_key_prefix
        keys = await self._backend.scan_keys(f"{prefix}*")
        return [key[len(prefix) :] for key in keys]

    async def reconcile_user_from_active_count(self, user_id: str, count: int) -> None:
        changed = await self._backend.decrease_to(user_key(self._settings, user_id), count)
        log.info(
            "semaphore_user_reconciled", user_id=user_id, active_count=count, changed=changed
        )
