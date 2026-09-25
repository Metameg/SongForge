"""Edge tests for POST /create (issue #12, criterion #1: identity cookie + QUEUED
persist before any external call; criterion #5: NOTIFY after persist).

Drives the FastAPI app through real HTTP (`TestClient`) with an in-memory SQLite
session override, mirroring `tests/test_now_playing.py`'s dependency-injection pattern
(`get_session`, spy overrides) so criteria are observable at the HTTP edge.

NOTE on `Job.seq` (Postgres `Identity`, see `.orchestrator/CONTEXT.md` and the `Job`
model docstring in `songforge/models.py`): confirmed experimentally that SQLite has no
server-side generator for a non-PK identity column -- inserting a `Job` without an
explicit `seq` raises a NOT NULL violation on SQLite even though the identical insert
works fine against real Postgres (the server generates it there). Rather than move
ALL create-route assertions to `@pytest.mark.integration` (real PG is slow, and this
route's actual behaviour under test here -- cookie mint/reuse, prompt validation, the
"no external call" boundary, NOTIFY-after-persist ordering -- has nothing to do with
`seq` specifically), this file installs a TEST-ONLY SQLAlchemy `before_insert` listener
that assigns `seq` from a local counter when unset. It is registered/removed around
each test (autouse fixture) so it never leaks into other test modules -- in particular
`tests/test_jobs_queue_integration.py`, which must observe genuine Postgres-generated
`seq` values for its FIFO-ordering assertions, not this shim's.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings, get_settings
from songforge.jobs import generation_client
from songforge.models import JOB_STATE_QUEUED, Base, Job
from songforge.web.app import create_app
from songforge.web.identity import mint, sign, unsign
from songforge.web.routes import create as create_route
from songforge.web.routes.create import get_notify_dependency, get_session

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Iterator[None]:
    """See module docstring: SQLite can't server-generate `Job.seq` (Postgres
    `Identity`), so this fills it in for the duration of this file's tests only."""
    event.listen(Job, "before_insert", _assign_test_seq)
    yield
    event.remove(Job, "before_insert", _assign_test_seq)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


class _NotifySpy:
    """Records `(channel, payload)` calls; injected via `get_notify_dependency`
    override and also logs onto a shared `events` list for ordering assertions."""

    def __init__(self, events: list[str]) -> None:
        self.calls: list[tuple[str, str]] = []
        self._events = events

    async def __call__(self, channel: str, payload: str) -> None:
        self.calls.append((channel, payload))
        self._events.append(f"notify:{channel}:{payload}")


class _CommitTrackingSession:
    """Thin passthrough proxy that logs `commit()` calls onto a shared `events` list,
    so tests can assert NOTIFY fires AFTER the persisting commit (criteria #1 + #5
    ordering) without coupling to the route's internal implementation details."""

    def __init__(self, session: AsyncSession, events: list[str]) -> None:
        self._session = session
        self._events = events

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def commit(self) -> None:
        await self._session.commit()
        self._events.append("committed")


def _build_client(
    sessionmaker: async_sessionmaker[AsyncSession], events: list[str]
) -> tuple[TestClient, _NotifySpy]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield _CommitTrackingSession(session, events)  # type: ignore[misc]

    notify_spy = _NotifySpy(events)
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
    return TestClient(app), notify_spy


async def _job_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Job]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(Job))).all())


async def test_create_without_cookie_mints_identity_and_persists_queued_job(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a synthwave song about the sea"})

    assert resp.status_code == 200
    assert "sf_uid" in resp.cookies
    body = resp.json()
    assert body["state"] == JOB_STATE_QUEUED

    rows = await _job_rows(sessionmaker)
    assert len(rows) == 1
    assert rows[0].state == JOB_STATE_QUEUED
    assert rows[0].job_id == body["job_id"]
    assert rows[0].user_id  # some non-empty minted identity


async def test_create_with_valid_cookie_reuses_identity_without_cookie_churn(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)
    existing_user_id = mint()
    signed = sign(existing_user_id, secret=get_settings().session_secret)
    client.cookies.set("sf_uid", signed)

    resp = client.post("/create", json={"prompt": "a quiet piano piece"})

    assert resp.status_code == 200
    assert "sf_uid" not in resp.cookies  # no re-mint / cookie churn on a valid cookie
    rows = await _job_rows(sessionmaker)
    assert rows[0].user_id == existing_user_id


async def test_create_with_tampered_cookie_is_rejected_and_remints_identity(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tampered signature must be treated exactly like no cookie at all -- rejected,
    not trusted, and a fresh identity minted with a freshly-signed cookie (criterion
    #1's "reject-and-remint on bad signature"; the original user id must never leak
    into the persisted job)."""
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)
    original_user_id = mint()
    signed = sign(original_user_id, secret=get_settings().session_secret)
    flipped_last_char = "0" if signed[-1] != "0" else "1"
    tampered = signed[:-1] + flipped_last_char
    client.cookies.set("sf_uid", tampered)

    resp = client.post("/create", json={"prompt": "a tampered-cookie song"})

    assert resp.status_code == 200
    assert "sf_uid" in resp.cookies  # re-minted -- the tampered cookie was not reused
    rows = await _job_rows(sessionmaker)
    assert rows[0].user_id != original_user_id
    reminted = unsign(resp.cookies["sf_uid"], secret=get_settings().session_secret)
    assert reminted == rows[0].user_id


async def test_create_with_cookie_signed_by_a_different_secret_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A cookie signed by an old/wrong secret (e.g. a rotated `session_secret`, or a
    forgery attempt) must be rejected exactly like a tampered one, not trusted as that
    identity."""
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)
    original_user_id = mint()
    signed_wrong_secret = sign(original_user_id, secret="a-completely-different-secret")
    client.cookies.set("sf_uid", signed_wrong_secret)

    resp = client.post("/create", json={"prompt": "a wrong-secret cookie song"})

    assert resp.status_code == 200
    assert "sf_uid" in resp.cookies  # re-minted
    rows = await _job_rows(sessionmaker)
    assert rows[0].user_id != original_user_id


async def test_create_never_calls_the_generation_client(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion #1: the job is QUEUED before any external call -- POST /create must
    never touch the generation API at all (that is `songforge.jobs.dispatch`'s job).
    Patches the real client's `create` to explode if ever invoked, as a boundary guard
    against a future regression coupling this route to the generation client."""

    async def _explode(*args: object, **kwargs: object) -> Any:
        raise AssertionError("POST /create must never call the generation client")

    monkeypatch.setattr(
        generation_client.HttpGenerationClient, "create", _explode, raising=True
    )
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a rock anthem"})

    assert resp.status_code == 200
    rows = await _job_rows(sessionmaker)
    assert rows[0].state == JOB_STATE_QUEUED  # persisted; the patched client never fired


async def test_create_emits_notify_after_the_persisting_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    client, notify_spy = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a lullaby"})

    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert events == ["committed", f"notify:new_job:{job_id}"]
    assert notify_spy.calls == [("new_job", job_id)]


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "x" * 100_000],
    ids=["empty", "whitespace-only", "oversized"],
)
async def test_create_rejects_invalid_prompt_and_persists_nothing(
    sessionmaker: async_sessionmaker[AsyncSession], prompt: str
) -> None:
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": prompt})

    assert resp.status_code == 422
    assert await _job_rows(sessionmaker) == []


async def test_create_rejects_oversized_lyrics_and_persists_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Security report MEDIUM finding: `lyrics` is the sibling of `prompt` in the
    same attacker-controlled, unauthenticated request body and must be capped the
    same way -- an unbounded `lyrics` field is the same resource-abuse vector
    (oversized DB rows + oversized outbound generation-API bodies) the prompt cap
    exists to close."""
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)
    oversized_lyrics = "x" * (get_settings().lyrics_max_length + 1)

    resp = client.post(
        "/create", json={"prompt": "a normal prompt", "lyrics": oversized_lyrics}
    )

    assert resp.status_code == 422
    assert await _job_rows(sessionmaker) == []


async def test_create_accepts_lyrics_at_the_max_length(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)
    max_lyrics = "x" * get_settings().lyrics_max_length

    resp = client.post("/create", json={"prompt": "a normal prompt", "lyrics": max_lyrics})

    assert resp.status_code == 200
    rows = await _job_rows(sessionmaker)
    assert len(rows) == 1
    assert rows[0].lyrics == max_lyrics


# ── Identity cookie `Secure` flag (security report MEDIUM finding) ────────────────
#
# The identity cookie is the sole identity/auth token; it must carry `Secure`
# outside local dev (HTTP is expected/allowed only in local dev), config-driven off
# `settings.environment`.


async def test_identity_cookie_lacks_secure_flag_in_local(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a song"})

    assert resp.status_code == 200
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "sf_uid" in set_cookie_header
    assert "secure" not in set_cookie_header.lower()



# ── Rate limiting + bot check gates (issue #15, criteria #1 + #3 + #4) ────────────
#
# `get_rate_limiter`/`get_bot_check` don't exist on `songforge.web.routes.create` yet,
# nor does the gating logic in `create_job` itself -- imported locally inside each
# test/helper below (mirrors `tests/test_now_playing.py`'s NOTE-documented convention)
# so these RED tests don't break the pre-existing tests above in this file.


def _build_client_with_gates(
    sessionmaker: async_sessionmaker[AsyncSession],
    events: list[str],
    *,
    rate_limiter: Any,
    bot_check: Any,
) -> tuple[TestClient, _NotifySpy]:
    from songforge.web.routes.create import get_bot_check, get_rate_limiter

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield _CommitTrackingSession(session, events)  # type: ignore[misc]

    notify_spy = _NotifySpy(events)
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    app.dependency_overrides[get_bot_check] = lambda: bot_check
    return TestClient(app), notify_spy


class _PassingBotCheck:
    async def verify(self, request: Any) -> bool:
        return True


class _FailingBotCheck:
    async def verify(self, request: Any) -> bool:
        return False


async def test_create_is_blocked_with_429_when_the_cookie_cap_is_exceeded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.web.rate_limit import RateLimitDecision

    class _CookieDenyingLimiter:
        async def consume(self, identity: Any, ip: str) -> RateLimitDecision:
            return RateLimitDecision(allowed=False, blocked_scope="cookie", remaining=0)

        async def remaining(self, identity: Any, ip: str) -> int:
            return 0

        async def refund(self, identity: Any, ip: str) -> None:
            return None

    events: list[str] = []
    client, _ = _build_client_with_gates(
        sessionmaker,
        events,
        rate_limiter=_CookieDenyingLimiter(),
        bot_check=_PassingBotCheck(),
    )

    resp = client.post("/create", json={"prompt": "one too many songs"})

    assert resp.status_code == 429
    assert resp.json()["detail"]  # friendly body, not empty
    assert await _job_rows(sessionmaker) == []


async def test_create_is_blocked_with_429_when_the_ip_cap_is_exceeded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.web.rate_limit import RateLimitDecision

    class _IpDenyingLimiter:
        async def consume(self, identity: Any, ip: str) -> RateLimitDecision:
            return RateLimitDecision(allowed=False, blocked_scope="ip", remaining=0)

        async def remaining(self, identity: Any, ip: str) -> int:
            return 0

        async def refund(self, identity: Any, ip: str) -> None:
            return None

    events: list[str] = []
    client, _ = _build_client_with_gates(
        sessionmaker, events, rate_limiter=_IpDenyingLimiter(), bot_check=_PassingBotCheck()
    )

    resp = client.post("/create", json={"prompt": "another ip-capped song"})

    assert resp.status_code == 429
    assert await _job_rows(sessionmaker) == []


async def test_create_is_blocked_with_403_when_the_bot_check_fails(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.web.rate_limit import RateLimitDecision

    class _AllowingLimiter:
        async def consume(self, identity: Any, ip: str) -> RateLimitDecision:
            return RateLimitDecision(allowed=True, blocked_scope=None, remaining=5)

        async def remaining(self, identity: Any, ip: str) -> int:
            return 5

        async def refund(self, identity: Any, ip: str) -> None:
            return None

    events: list[str] = []
    client, _ = _build_client_with_gates(
        sessionmaker, events, rate_limiter=_AllowingLimiter(), bot_check=_FailingBotCheck()
    )

    resp = client.post("/create", json={"prompt": "a bot's song"})

    assert resp.status_code == 403
    assert await _job_rows(sessionmaker) == []


async def test_create_succeeds_within_caps_and_increments_the_limiter_counter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from songforge.web.rate_limit import RateLimitDecision

    class _RecordingLimiter:
        def __init__(self) -> None:
            self.consume_calls: list[tuple[str, str]] = []

        async def consume(self, identity: Any, ip: str) -> RateLimitDecision:
            self.consume_calls.append((identity.user_id, ip))
            return RateLimitDecision(allowed=True, blocked_scope=None, remaining=1)

        async def remaining(self, identity: Any, ip: str) -> int:
            return 1

        async def refund(self, identity: Any, ip: str) -> None:
            return None

    events: list[str] = []
    limiter = _RecordingLimiter()
    client, _ = _build_client_with_gates(
        sessionmaker, events, rate_limiter=limiter, bot_check=_PassingBotCheck()
    )

    resp = client.post("/create", json={"prompt": "a song under the cap"})

    assert resp.status_code == 200
    rows = await _job_rows(sessionmaker)
    assert len(rows) == 1
    assert len(limiter.consume_calls) == 1  # the counter was actually incremented


async def test_enforce_rate_limits_false_never_blocks_and_bypasses_bot_check(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global kill switch (spec #29): with enforcement off, creation must never be
    blocked by the daily caps OR the bot check -- exercised against the REAL default
    rate-limiter/bot-check dependencies (no fakes), proving the disabled posture
    end-to-end. This needs no live Redis: the disabled decision layer never touches its
    backend at all (see `tests/test_rate_limit.py::test_enforce_rate_limits_false_*`),
    and the Redis client itself only connects lazily on first command (see
    `redis_client.py`'s docstring / `web/app.py`'s `PointerBroadcaster` construction)."""
    disabled_settings = Settings(_env={**os.environ, "ENFORCE_RATE_LIMITS": "false"})
    monkeypatch.setattr(create_route, "get_settings", lambda: disabled_settings)

    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    responses = [
        client.post("/create", json={"prompt": f"song number {i}"}) for i in range(5)
    ]

    assert all(resp.status_code == 200 for resp in responses)
    rows = await _job_rows(sessionmaker)
    assert len(rows) == 5



# ── Persisted identity for the refund seam (issue #16, design D3) ────────────────
#
# The watchdog's terminal-failure sweep needs the EXACT `client_ip`/`is_authenticated`
# that created a job to reconstruct an accurate `RateLimiter.refund` call -- an anon
# create charges BOTH the cookie AND the IP counters, but the job row previously only
# stored `user_id`. `Job.client_ip`/`Job.is_authenticated` exist now (nullable/
# defaulted, see `models.py`), but `create_job` doesn't set them yet -- RED.


async def test_create_persists_client_ip_and_is_authenticated_for_the_refund_seam(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        create_route, "client_ip", lambda request, settings: "203.0.113.9"
    )
    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a song for the watchdog"})

    assert resp.status_code == 200
    rows = await _job_rows(sessionmaker)
    assert rows[0].client_ip == "203.0.113.9"
    assert rows[0].is_authenticated is False


@pytest.mark.parametrize("environment", ["staging", "prod"])
async def test_identity_cookie_has_secure_flag_outside_local(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    # A real (non-placeholder) secret so `environment == "prod"` doesn't trip the
    # fail-closed guard added for the config MEDIUM finding -- unrelated to what
    # this test is proving.
    non_local_settings = Settings(
        _env={**os.environ, "ENVIRONMENT": environment, "SESSION_SECRET": "a-real-secret"}
    )
    monkeypatch.setattr(create_route, "get_settings", lambda: non_local_settings)

    events: list[str] = []
    client, _ = _build_client(sessionmaker, events)

    resp = client.post("/create", json={"prompt": "a song"})

    assert resp.status_code == 200
    set_cookie_header = resp.headers.get("set-cookie", "")
    assert "sf_uid" in set_cookie_header
    assert "secure" in set_cookie_header.lower()
