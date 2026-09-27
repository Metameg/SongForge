"""Unit tests for ``RateLimiter.check()`` + ``charge()`` -- the split ``POST /create``
uses to charge a quota slot only AFTER the job row is durably committed.

Durability-boundary fix: the old flow charged the slot (``consume``) BEFORE the
Postgres commit, so an instance that crashed in between leaked a slot with no job and
no way to refund it (the refund path keys off the persisted row). The new flow does a
read-only ``check()`` before the write (reject cleanly, charge nothing) and an
unconditional ``charge()`` after the durable commit. The trade-off is a bounded
over-admission race (two concurrent creates can each pass ``check`` before either
charges) -- acceptable under the deterrence-not-prevention quota stance, and it errs
toward the user, never toward wasted generation spend.

File-local in-memory fake backend, mirroring ``tests/test_rate_limit.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from songforge.config import Settings
from songforge.web.rate_limit import (
    Identity,
    RateLimiter,
    account_key,
    cookie_key,
    ip_key,
)


class _InMemoryRateLimitBackend:
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
    overrides.setdefault("enforce_rate_limits", True)
    return Settings(**overrides)  # type: ignore[arg-type]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _anon(user_id: str = "u1") -> Identity:
    return Identity(user_id=user_id, is_authenticated=False, minted=False)


def _authed(user_id: str = "acct1") -> Identity:
    return Identity(user_id=user_id, is_authenticated=True, minted=False)


# ── check(): read-only allow/deny, never increments ──────────────────────────────


async def test_check_allows_under_cap_without_incrementing() -> None:
    backend = _InMemoryRateLimitBackend()
    rl = RateLimiter(
        backend, _settings(anon_daily_songs_per_cookie=3, anon_daily_songs_per_ip=3)
    )

    decision = await rl.check(_anon(), "1.2.3.4")

    assert decision.allowed is True
    assert decision.blocked_scope is None
    assert decision.remaining == 3
    assert backend._counts == {}  # read-only -- nothing charged


async def test_check_denies_when_cookie_scope_at_cap_and_names_it() -> None:
    backend = _InMemoryRateLimitBackend()
    s = _settings(anon_daily_songs_per_cookie=1, anon_daily_songs_per_ip=5)
    rl = RateLimiter(backend, s)
    await backend.incr_with_expiry(cookie_key(s, "u1", _today()), 60)  # at cap

    decision = await rl.check(_anon("u1"), "1.2.3.4")

    assert decision.allowed is False
    assert decision.blocked_scope == "cookie"
    assert decision.remaining == 0


async def test_check_denies_when_ip_scope_at_cap() -> None:
    backend = _InMemoryRateLimitBackend()
    s = _settings(anon_daily_songs_per_cookie=5, anon_daily_songs_per_ip=1)
    rl = RateLimiter(backend, s)
    await backend.incr_with_expiry(ip_key(s, "9.9.9.9", _today()), 60)  # at cap

    decision = await rl.check(_anon("u1"), "9.9.9.9")

    assert decision.allowed is False
    assert decision.blocked_scope == "ip"


async def test_check_enforcement_off_allows_without_touching_backend() -> None:
    backend = _InMemoryRateLimitBackend()
    rl = RateLimiter(backend, _settings(enforce_rate_limits=False))

    decision = await rl.check(_anon(), "1.2.3.4")

    assert decision.allowed is True
    assert backend._counts == {}


# ── charge(): unconditional per-scope increment for a now-durable job ─────────────


async def test_charge_increments_all_anon_scopes_with_expiry() -> None:
    backend = _InMemoryRateLimitBackend()
    s = _settings(rate_limit_window_seconds=3600)
    rl = RateLimiter(backend, s)

    await rl.charge(_anon("u1"), "1.2.3.4")

    assert await backend.get_count(cookie_key(s, "u1", _today())) == 1
    assert await backend.get_count(ip_key(s, "1.2.3.4", _today())) == 1
    assert backend.ttls_seen[cookie_key(s, "u1", _today())] == 3600


async def test_charge_does_not_roll_back_and_may_exceed_cap() -> None:
    # Post-commit the job is durable, so charge is unconditional: no cap check, no
    # rollback. Over-admission (count > cap) is the accepted, bounded trade-off.
    backend = _InMemoryRateLimitBackend()
    s = _settings(anon_daily_songs_per_cookie=1, anon_daily_songs_per_ip=1)
    rl = RateLimiter(backend, s)

    await rl.charge(_anon("u1"), "1.2.3.4")
    await rl.charge(_anon("u1"), "1.2.3.4")

    assert await backend.get_count(cookie_key(s, "u1", _today())) == 2  # exceeded, no rollback


async def test_charge_enforcement_off_is_a_noop() -> None:
    backend = _InMemoryRateLimitBackend()
    rl = RateLimiter(backend, _settings(enforce_rate_limits=False))

    await rl.charge(_anon(), "1.2.3.4")

    assert backend._counts == {}


async def test_authenticated_identity_checks_and_charges_account_scope_only() -> None:
    backend = _InMemoryRateLimitBackend()
    s = _settings(authed_daily_songs=10)
    rl = RateLimiter(backend, s)

    decision = await rl.check(_authed("acct1"), "1.2.3.4")
    assert decision.allowed is True

    await rl.charge(_authed("acct1"), "1.2.3.4")

    assert await backend.get_count(account_key(s, "acct1", _today())) == 1
    # IP-exempt: the IP counter is never touched for an authenticated identity.
    assert await backend.get_count(ip_key(s, "1.2.3.4", _today())) == 0
