"""Daily-quota rate limiter: per-cookie / per-IP / per-account song caps (issue #15).

Satisfies criterion #1 (anonymous creation capped per signed cookie AND -- more loosely
-- per IP; authenticated users are IP-exempt and run on their account limit) and
criterion #4 (all limits come from the single config source and are disabled by
``ENFORCE_RATE_LIMITS=false``).

Split, exactly like ``songforge.jobs.semaphore``, into a pure cap-decision layer
(``RateLimiter``) and an atomic counter-primitive backend
(``RateLimitBackend``/``RedisRateLimitBackend``, Lua-script backed for true
cross-process atomicity). ``RateLimiter`` unit-tests against a file-local in-memory fake
backend (``tests/test_rate_limit.py``); the real Lua path is exercised only where a live
Redis is available (integration).

The anonymous rule mirrors ``RedisSemaphore.acquire``'s "increment global AND per-user
together; roll back the other leg if either would exceed" pairing: an anonymous
``consume`` must satisfy BOTH the per-cookie AND per-IP caps, and rolls back a partial
increment if the second leg is over cap -- so a rejected create never silently
pre-charges a quota slot (the same "no leaked leg" property the semaphore guarantees).
Authenticated identities (the accounts seam; ``is_authenticated`` is always ``False``
until a future accounts issue -- see ``.orchestrator/CONTEXT.md``) are IP-exempt and run
on ``authed_daily_songs`` alone.

Redis is derived/ephemeral (see ``redis_client.py``): the counters rebuild from an empty
state each window and carry a per-day UTC bucket in the key, so a lost counter costs at
most a partial day's over-count, never a permanent quota corruption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from redis.asyncio import Redis

from songforge.config import Settings
from songforge.logging_setup import get_logger
from songforge.metrics import rate_limit_rejected_total

log = get_logger(__name__)

# KEYS[1] = the counter key. ARGV[1] = TTL seconds. Atomically increments and, ONLY on
# the first increment (result == 1), stamps the window expiry -- so a later request in
# the same window never resets the TTL and extends the window indefinitely. EVAL-based
# for the same reason as the semaphore's `_TRY_INCREMENT_LUA`: "increment, and set TTL
# iff this was the first increment" must be one atomic step across concurrent clients.
_INCR_WITH_EXPIRY_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""

# KEYS[1] = the counter key. Floors at 0 (like the semaphore's `_DECREMENT_LUA`): a
# refund with no matching increment, or a double-refund, must never take a counter
# negative and hand back quota that was never spent.
_DECREMENT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
  redis.call('DECR', KEYS[1])
end
return 1
"""


@dataclass(frozen=True)
class Identity:
    """A resolved request identity. ``is_authenticated`` is always ``False`` for now --
    the accounts seam (no accounts system exists yet; see ``.orchestrator/CONTEXT.md``).
    ``minted`` marks an identity freshly created this request (the caller sets its
    signed cookie on the response)."""

    user_id: str
    is_authenticated: bool
    minted: bool


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a ``consume``: whether it was allowed, which scope blocked it (if
    any), and the "songs left" figure to surface to the client."""

    allowed: bool
    blocked_scope: str | None
    remaining: int


def cookie_key(settings: Settings, user_id: str, day: str) -> str:
    """Redis key for one anonymous cookie identity's daily counter, bucketed by day."""
    return f"{settings.rate_limit_cookie_prefix}{user_id}:{day}"


def ip_key(settings: Settings, ip: str, day: str) -> str:
    """Redis key for one IP's daily counter, bucketed by day."""
    return f"{settings.rate_limit_ip_prefix}{ip}:{day}"


def account_key(settings: Settings, user_id: str, day: str) -> str:
    """Redis key for one authenticated account's daily counter, bucketed by day."""
    return f"{settings.rate_limit_account_prefix}{user_id}:{day}"


class RateLimitBackend(Protocol):
    """Atomic counter primitives the cap decision (``RateLimiter``) is built on.

    Production (``RedisRateLimitBackend``) implements these via Redis Lua scripts for
    true cross-process atomicity. Tests substitute a simple in-process fake (see
    ``tests/test_rate_limit.py``) -- safe because a Python coroutine with no internal
    ``await`` never yields mid-body, so each call is atomic with respect to concurrent
    ``asyncio.gather`` callers (same rationale as ``jobs/semaphore.py``'s fake).
    """

    async def incr_with_expiry(self, key: str, ttl_seconds: int) -> int:
        """Atomically increment ``key``; on the first increment, set its ``ttl_seconds``
        expiry. Returns the new count."""
        ...

    async def get_count(self, key: str) -> int:
        """Return the current count for ``key`` (0 if unset)."""
        ...

    async def decr(self, key: str) -> None:
        """Atomically decrement ``key``, floored at 0 (a refund must never go negative)."""
        ...


class RedisRateLimitBackend:
    """Real backend: Redis-native atomic ops via Lua scripts (cross-process atomicity)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def incr_with_expiry(self, key: str, ttl_seconds: int) -> int:
        result = await self._redis.eval(_INCR_WITH_EXPIRY_LUA, 1, key, ttl_seconds)
        return int(result)

    async def get_count(self, key: str) -> int:
        value = await self._redis.get(key)
        return int(value) if value is not None else 0

    async def decr(self, key: str) -> None:
        await self._redis.eval(_DECREMENT_LUA, 1, key)


def _today() -> str:
    """UTC date bucket string (ISO-8601 ``YYYY-MM-DD``) -- the day a counter belongs to.

    UTC (not ``date.today()``, which reads the local system timezone) so every worker /
    web instance agrees on when "today" rolls over regardless of server locale.
    """
    return datetime.now(timezone.utc).date().isoformat()


@dataclass(frozen=True)
class _Scope:
    """One cap that applies to a ``consume``: its name, Redis key, and limit."""

    name: str
    key: str
    cap: int


class RateLimiter:
    """Pure daily-cap decision layer over a ``RateLimitBackend`` (issue #15, #1/#4)."""

    def __init__(self, backend: RateLimitBackend, settings: Settings) -> None:
        self._backend = backend
        self._settings = settings

    def _scopes(self, identity: Identity, ip: str, day: str) -> list[_Scope]:
        """The caps that apply to ``identity``: an authenticated account is IP-exempt and
        runs on the account cap alone; an anonymous identity must satisfy BOTH the
        per-cookie and per-IP caps."""
        settings = self._settings
        if identity.is_authenticated:
            return [
                _Scope(
                    "account",
                    account_key(settings, identity.user_id, day),
                    settings.authed_daily_songs,
                )
            ]
        return [
            _Scope(
                "cookie",
                cookie_key(settings, identity.user_id, day),
                settings.anon_daily_songs_per_cookie,
            ),
            _Scope("ip", ip_key(settings, ip, day), settings.anon_daily_songs_per_ip),
        ]

    def _cap(self, identity: Identity) -> int:
        """The tighter applicable cap -- the figure "songs left" is measured against
        when enforcement is off (usage ignored, the configured ceiling reported)."""
        settings = self._settings
        if identity.is_authenticated:
            return settings.authed_daily_songs
        return min(settings.anon_daily_songs_per_cookie, settings.anon_daily_songs_per_ip)

    async def consume(self, identity: Identity, ip: str) -> RateLimitDecision:
        """Atomically charge one song against every applicable cap.

        Increments each applicable counter in turn; if any would exceed its cap, rolls
        back every increment already made this call and returns a denied decision naming
        the blocking scope (mirrors ``RedisSemaphore.acquire``'s paired rollback).
        Returns ``allowed=True`` with the "songs left" figure otherwise. When
        ``enforce_rate_limits`` is off, always allows and never touches the backend.
        """
        if not self._settings.enforce_rate_limits:
            return RateLimitDecision(
                allowed=True, blocked_scope=None, remaining=self._cap(identity)
            )

        scopes = self._scopes(identity, ip, _today())
        incremented: list[str] = []
        remainders: list[int] = []
        for scope in scopes:
            count = await self._backend.incr_with_expiry(
                scope.key, self._settings.rate_limit_window_seconds
            )
            incremented.append(scope.key)
            if count > scope.cap:
                for key in incremented:
                    await self._backend.decr(key)
                rate_limit_rejected_total.labels(scope=scope.name).inc()
                log.info(
                    "rate_limit_denied", scope=scope.name, user_id=identity.user_id
                )
                return RateLimitDecision(
                    allowed=False, blocked_scope=scope.name, remaining=0
                )
            remainders.append(scope.cap - count)
        return RateLimitDecision(
            allowed=True, blocked_scope=None, remaining=min(remainders)
        )

    async def remaining(self, identity: Identity, ip: str) -> int:
        """Read-only "songs left": the tighter of the applicable caps minus current use
        (floored at 0), with no increment. When enforcement is off, returns the
        configured cap (usage ignored)."""
        if not self._settings.enforce_rate_limits:
            return self._cap(identity)
        scopes = self._scopes(identity, ip, _today())
        values: list[int] = []
        for scope in scopes:
            count = await self._backend.get_count(scope.key)
            values.append(max(scope.cap - count, 0))
        return min(values)

    async def refund(self, identity: Identity, ip: str) -> None:
        """Hand back one previously-consumed slot on every applicable counter (floored
        at 0). Called by the failure-recovery path (issue #16) when a generation fails,
        so a failure never costs the user a quota slot. No-op when enforcement is off
        (nothing was ever charged)."""
        if not self._settings.enforce_rate_limits:
            return
        for scope in self._scopes(identity, ip, _today()):
            await self._backend.decr(scope.key)
        log.info("rate_limit_refunded", user_id=identity.user_id)
