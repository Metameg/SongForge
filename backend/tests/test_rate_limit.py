"""Unit tests for the daily-quota rate limiter (issue #15, criteria #1 + #4).

Exercises the pure decision layer (``RateLimiter``) against a file-local in-memory fake
``RateLimitBackend`` -- no live Redis needed, mirroring ``tests/test_semaphore.py``'s
fake-backend convention (see ``songforge/jobs/semaphore.py``'s module docstring for why
a fake backend with no internal ``await`` is already atomic under ``asyncio.gather``).

Production change that turns these green: implementing ``songforge/web/rate_limit.py``
(``RateLimitBackend`` protocol, ``cookie_key``/``ip_key``/``account_key``, ``Identity``,
``RateLimitDecision``, ``RateLimiter``). This whole module does not exist yet -- every
test below is RED at collection time (``ModuleNotFoundError``), which is the correct RED
signature per the TDD process for a brand-new module.

Anonymous cap rule mirrors ``RedisSemaphore.acquire``'s "acquire global AND per-user
together; roll back the other leg if either fails" pairing: an anonymous ``consume``
must satisfy BOTH the per-cookie AND per-IP caps, rolling back a partial increment if
the other scope's cap is exceeded. Authenticated users (the accounts seam,
``is_authenticated`` always ``False`` for now -- see ``.orchestrator/CONTEXT.md``) are
IP-exempt and run on the account cap alone.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from songforge.config import Settings
from songforge.web.rate_limit import (
    Identity,
    RateLimiter,
    account_key,
    cookie_key,
    ip_key,
)


class _InMemoryRateLimitBackend:
    """File-local fake ``RateLimitBackend``: plain dict counters, no real Redis."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self.ttls_seen: dict[str, int] = {}

    async def incr_with_expiry(self, key: str, ttl_seconds: int) -> int:
        is_first = key not in self._counts
        self._counts[key] = self._counts.get(key, 0) + 1
        if is_first:
            self.ttls_seen[key] = ttl_seconds
        return self._counts[key]

    async def get_count(self, key: str) -> int:
        return self._counts.get(key, 0)

    async def decr(self, key: str) -> None:
        self._counts[key] = max(self._counts.get(key, 0) - 1, 0)


def _settings(**overrides: object) -> Settings:
    # This suite exercises enforcement LOGIC, so it opts back into enforcement (the
    # suite-wide default is OFF -- see `conftest.py`); the `enforce_rate_limits=False`
    # tests below override this explicitly.
    overrides.setdefault("enforce_rate_limits", True)
    return Settings(**overrides)  # type: ignore[arg-type]


def _today() -> str:
    """UTC date bucket string -- the PRD describes ``day`` as "a UTC date string
    bucket"; this assumes the standard ISO-8601 ``date().isoformat()`` form
    (``RateLimiter`` computes "today" internally -- ``consume``/``remaining``/``refund``
    take no explicit ``day`` argument). Used only by the handful of tests below that
    introspect the backend directly (mirrors ``test_semaphore.py``'s
    ``backend.get(global_key(settings))`` style) to confirm atomic rollback / IP
    exemption; every other test asserts purely through the public
    ``consume``/``remaining``/``refund`` contract and does not depend on this format."""
    return datetime.now(timezone.utc).date().isoformat()


# ── Key builders ──────────────────────────────────────────────────────────────────


def test_key_builders_produce_distinct_keys_per_scope() -> None:
    settings = _settings()
    day = "2026-09-19"

    cookie = cookie_key(settings, "user-a", day)
    ip = ip_key(settings, "203.0.113.5", day)
    account = account_key(settings, "user-a", day)

    assert len({cookie, ip, account}) == 3
    assert "user-a" in cookie
    assert "203.0.113.5" in ip
    assert "user-a" in account
    assert day in cookie
    assert day in ip
    assert day in account


def test_key_builders_bucket_counters_by_day() -> None:
    settings = _settings()

    assert cookie_key(settings, "user-a", "2026-09-19") != cookie_key(
        settings, "user-a", "2026-09-20"
    )


# ── Identity dataclass ───────────────────────────────────────────────────────────


def test_identity_dataclass_holds_expected_fields() -> None:
    identity = Identity(user_id="user-a", is_authenticated=False, minted=True)

    assert identity.user_id == "user-a"
    assert identity.is_authenticated is False
    assert identity.minted is True


# ── consume(): anonymous cap rule ───────────────────────────────────────────────


async def test_consume_allows_anonymous_under_both_caps_and_reports_remaining() -> None:
    settings = _settings(anon_daily_songs_per_cookie=2, anon_daily_songs_per_ip=6)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)

    decision = await limiter.consume(identity, "203.0.113.5")

    assert decision.allowed is True
    assert decision.blocked_scope is None
    assert decision.remaining == 1  # min(2-1, 6-1)


async def test_consume_denies_with_cookie_scope_when_cookie_cap_exceeded() -> None:
    settings = _settings(anon_daily_songs_per_cookie=1, anon_daily_songs_per_ip=100)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"
    await limiter.consume(identity, ip)

    decision = await limiter.consume(identity, ip)

    assert decision.allowed is False
    assert decision.blocked_scope == "cookie"
    assert decision.remaining == 0


async def test_consume_denies_with_ip_scope_when_ip_cap_exceeded_and_rolls_back_the_cookie_counter() -> (
    None
):
    """The anonymous cap rule requires BOTH the cookie AND ip caps to hold (mirrors
    ``RedisSemaphore.acquire``'s global+per-user pairing). If the ip leg fails after the
    cookie leg already incremented, the cookie increment must be rolled back -- not left
    to silently pre-charge a quota slot for a create that was never allowed (the same
    "no leaked leg" property ``test_semaphore.py::test_failed_per_user_acquire_does_not_leak_a_global_slot``
    locks for the semaphore)."""
    settings = _settings(anon_daily_songs_per_cookie=10, anon_daily_songs_per_ip=1)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    ip = "203.0.113.5"
    identity_a = Identity(user_id="user-a", is_authenticated=False, minted=False)
    identity_b = Identity(user_id="user-b", is_authenticated=False, minted=False)
    await limiter.consume(identity_a, ip)  # consumes the shared ip cap (1)

    decision = await limiter.consume(identity_b, ip)

    assert decision.allowed is False
    assert decision.blocked_scope == "ip"
    assert await backend.get_count(cookie_key(settings, "user-b", _today())) == 0


# ── consume(): authenticated seam (IP-exempt, account cap) ─────────────────────


async def test_authenticated_consume_uses_only_the_account_cap_and_is_ip_exempt() -> None:
    settings = _settings(anon_daily_songs_per_ip=1, authed_daily_songs=2)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    ip = "203.0.113.5"
    # Exhaust the shared IP cap via an anonymous identity first.
    anon_identity = Identity(user_id="anon-a", is_authenticated=False, minted=False)
    await limiter.consume(anon_identity, ip)
    authed_identity = Identity(user_id="acct-1", is_authenticated=True, minted=False)

    first = await limiter.consume(authed_identity, ip)
    second = await limiter.consume(authed_identity, ip)
    third = await limiter.consume(authed_identity, ip)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.blocked_scope == "account"
    # The IP counter is untouched by the authenticated (IP-exempt) consumes -- still
    # exactly what the anonymous identity alone put there.
    assert await backend.get_count(ip_key(settings, ip, _today())) == 1


# ── remaining() ──────────────────────────────────────────────────────────────────


async def test_remaining_is_the_tighter_of_the_applicable_caps() -> None:
    settings = _settings(anon_daily_songs_per_cookie=5, anon_daily_songs_per_ip=2)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"

    before = await limiter.remaining(identity, ip)
    await limiter.consume(identity, ip)
    after = await limiter.remaining(identity, ip)

    assert before == 2  # min(5, 2)
    assert after == 1  # min(4, 1)


# ── refund() ─────────────────────────────────────────────────────────────────────


async def test_refund_decrements_the_applicable_counters_floored_at_zero() -> None:
    settings = _settings(anon_daily_songs_per_cookie=5, anon_daily_songs_per_ip=5)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"
    await limiter.consume(identity, ip)

    await limiter.refund(identity, ip)
    remaining_after_one_refund = await limiter.remaining(identity, ip)
    await limiter.refund(identity, ip)  # double refund, no matching second consume
    remaining_after_double_refund = await limiter.remaining(identity, ip)

    assert remaining_after_one_refund == 5
    assert remaining_after_double_refund == 5  # floored, never "over-refunds"


async def test_refund_for_authenticated_identity_only_touches_the_account_counter() -> None:
    settings = _settings(authed_daily_songs=3)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="acct-1", is_authenticated=True, minted=False)
    ip = "203.0.113.5"
    await limiter.consume(identity, ip)

    await limiter.refund(identity, ip)

    assert await backend.get_count(ip_key(settings, ip, _today())) == 0
    assert await backend.get_count(account_key(settings, "acct-1", _today())) == 0


async def test_refund_with_an_explicit_day_decrements_that_days_bucket_not_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 (issue #16 phase 5 fix): a refund's day bucket must be the job's CHARGE day
    -- passed explicitly by a caller that knows it (``jobs.watchdog.
    sweep_terminal_failures``) -- not whatever ``_today()`` resolves to when the
    refund happens to run. Freezes "today" to a date AFTER the charged day and proves
    the explicit ``day=`` bucket is what gets decremented, while today's (never
    charged) bucket is untouched -- closing the cross-midnight farmable-gap an
    always-``_today()`` refund would leave open."""
    import songforge.web.rate_limit as rate_limit_module

    settings = _settings(anon_daily_songs_per_cookie=5, anon_daily_songs_per_ip=5)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"
    charged_day = "2026-01-01"

    # Charge directly against the charged-day bucket (bypassing `consume`, which
    # always charges "today") to set up the scenario: a slot was consumed on
    # `charged_day`, and the refund runs on a LATER "today".
    await backend.incr_with_expiry(
        cookie_key(settings, "user-a", charged_day), settings.rate_limit_window_seconds
    )
    await backend.incr_with_expiry(
        ip_key(settings, ip, charged_day), settings.rate_limit_window_seconds
    )

    monkeypatch.setattr(
        rate_limit_module,
        "_today",
        lambda: "2026-01-02",  # "now" is a day AFTER the charge
    )

    await limiter.refund(identity, ip, day=charged_day)

    assert await backend.get_count(cookie_key(settings, "user-a", charged_day)) == 0
    assert await backend.get_count(ip_key(settings, ip, charged_day)) == 0
    # Today's bucket (never charged) is untouched -- no farmable extra slot appears.
    assert await backend.get_count(cookie_key(settings, "user-a", "2026-01-02")) == 0
    assert await backend.get_count(ip_key(settings, ip, "2026-01-02")) == 0


async def test_refund_with_no_day_defaults_to_today_backward_compatible() -> None:
    """Backward compatibility: existing callers (e.g. issue #15's own tests) that call
    ``refund(identity, ip)`` with no ``day`` keep refunding "today"."""
    settings = _settings(anon_daily_songs_per_cookie=5, anon_daily_songs_per_ip=5)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"
    await limiter.consume(identity, ip)

    await limiter.refund(identity, ip)

    assert await backend.get_count(cookie_key(settings, "user-a", _today())) == 0
    assert await backend.get_count(ip_key(settings, ip, _today())) == 0


# ── enforce_rate_limits kill switch ──────────────────────────────────────────────


async def test_enforce_rate_limits_false_always_allows_and_never_increments() -> None:
    settings = _settings(
        enforce_rate_limits=False, anon_daily_songs_per_cookie=1, anon_daily_songs_per_ip=1
    )
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)
    ip = "203.0.113.5"

    decisions = [await limiter.consume(identity, ip) for _ in range(5)]

    assert all(decision.allowed for decision in decisions)
    assert await backend.get_count(cookie_key(settings, "user-a", _today())) == 0
    assert await backend.get_count(ip_key(settings, ip, _today())) == 0


async def test_enforce_rate_limits_false_remaining_returns_the_applicable_cap() -> None:
    settings = _settings(
        enforce_rate_limits=False, anon_daily_songs_per_cookie=2, anon_daily_songs_per_ip=6
    )
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)

    remaining = await limiter.remaining(identity, "203.0.113.5")

    assert remaining == 2  # min(2, 6) -- the tighter configured cap, usage ignored


# ── TTL / atomicity ──────────────────────────────────────────────────────────────


async def test_consume_sets_ttl_from_the_configured_window_on_first_increment() -> None:
    settings = _settings(rate_limit_window_seconds=3600)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    identity = Identity(user_id="user-a", is_authenticated=False, minted=False)

    await limiter.consume(identity, "203.0.113.5")

    assert backend.ttls_seen[cookie_key(settings, "user-a", _today())] == 3600


async def test_concurrent_consumes_never_exceed_the_ip_cap() -> None:
    settings = _settings(anon_daily_songs_per_cookie=100, anon_daily_songs_per_ip=3)
    backend = _InMemoryRateLimitBackend()
    limiter = RateLimiter(backend, settings)
    ip = "203.0.113.5"
    identities = [
        Identity(user_id=f"user-{i}", is_authenticated=False, minted=False)
        for i in range(10)
    ]

    decisions = await asyncio.gather(
        *(limiter.consume(identity, ip) for identity in identities)
    )

    assert sum(1 for decision in decisions if decision.allowed) == 3
    assert await backend.get_count(ip_key(settings, ip, _today())) == 3
