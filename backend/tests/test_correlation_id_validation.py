"""Inbound `X-Correlation-ID` validation (issue #18 fix-pass, security M2 + L1).

`CorrelationIdMiddleware` used to bind the inbound header verbatim
(`cid = incoming or uuid4().hex`), with no length or charset check. `POST /create`
persists that value into `jobs.correlation_id`, a `String(64)` column
(`songforge/models.py`), and `/create` is a public, anon-allowed endpoint -- so an
attacker-supplied header longer than 64 chars made the INSERT raise
`value too long for type character varying(64)` on Postgres (a client-controlled
500), and the value was also echoed verbatim on the response header and bound into
the log stream with no sanitization (L1, defense-in-depth).

Fix: sanitize to `[A-Za-z0-9._-]` and cap to 64 chars *before* binding, so both the
log stream and the DB column only ever receive a validated value; an all-illegal
header falls back to a freshly minted id rather than binding an empty string.

Mirrors `tests/test_web_app.py`'s no-datastore `/health` client for pure
sanitization behaviour, and `tests/test_correlation_trace.py`'s sessionmaker-backed
`/create` client for the one test that must prove the fix doesn't break job
creation end to end.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base, Job
from songforge.web.app import create_app
from songforge.web.middleware import _sanitize_correlation_id
from songforge.web.routes.create import get_notify_dependency, get_session

_HEX32 = re.compile(r"^[0-9a-f]{32}$")

client = TestClient(create_app())


# ── unit tests: the sanitizer itself ───────────────────────────────────────────


def test_sanitize_passes_a_normal_hex_id_through_unchanged() -> None:
    hex_id = "0123456789abcdef0123456789abcdef"
    assert _sanitize_correlation_id(hex_id) == hex_id


def test_sanitize_strips_illegal_characters() -> None:
    assert _sanitize_correlation_id("abc def<script>123") == "abcdefscript123"


def test_sanitize_strips_control_characters_like_crlf() -> None:
    assert _sanitize_correlation_id("a\r\nb") == "ab"


def test_sanitize_caps_to_64_characters() -> None:
    result = _sanitize_correlation_id("a" * 200)
    assert result == "a" * 64
    assert len(result) == 64


def test_sanitize_mints_a_fresh_id_when_nothing_legal_survives() -> None:
    result = _sanitize_correlation_id("   <<<>>>   ")
    assert _HEX32.match(result), f"expected a freshly minted uuid4().hex, got {result!r}"


def test_sanitize_mints_a_fresh_id_when_header_absent() -> None:
    result = _sanitize_correlation_id(None)
    assert _HEX32.match(result), f"expected a freshly minted uuid4().hex, got {result!r}"


# ── HTTP-level: the middleware wires the sanitizer in ──────────────────────────


def test_http_normal_hex_id_echoed_unchanged() -> None:
    hex_id = "0123456789abcdef0123456789abcdef"
    resp = client.get("/health", headers={"X-Correlation-ID": hex_id})
    assert resp.headers["X-Correlation-ID"] == hex_id


def test_http_oversized_header_is_capped_on_echo() -> None:
    resp = client.get("/health", headers={"X-Correlation-ID": "a" * 200})
    echoed = resp.headers["X-Correlation-ID"]
    assert echoed == "a" * 64
    assert len(echoed) <= 64


def test_http_illegal_characters_stripped_on_echo() -> None:
    resp = client.get("/health", headers={"X-Correlation-ID": "abc def<script>123"})
    assert resp.headers["X-Correlation-ID"] == "abcdefscript123"


def test_http_all_illegal_header_falls_back_to_minted_id() -> None:
    resp = client.get("/health", headers={"X-Correlation-ID": "   <<<>>>   "})
    echoed = resp.headers["X-Correlation-ID"]
    assert _HEX32.match(echoed), f"expected a freshly minted uuid4().hex, got {echoed!r}"


# ── end to end: an oversized header must not break job creation ───────────────

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Iterator[None]:
    """See `tests/test_create_route.py`'s module docstring: SQLite can't server-
    generate `Job.seq` (a Postgres `Identity`), so this fills it in for this file's
    tests only."""
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
    async def __call__(self, channel: str, payload: str) -> None:
        return None


def _build_client(sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    return TestClient(app)


async def test_create_with_oversized_header_persists_the_capped_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A 200-char inbound header must not 500 the create path (it would have
    overflowed `jobs.correlation_id String(64)` pre-fix) -- it persists the
    sanitized, capped id instead."""
    create_client = _build_client(sessionmaker)

    resp = create_client.post(
        "/create",
        json={"prompt": "a song traced end to end"},
        headers={"X-Correlation-ID": "b" * 200},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Correlation-ID"] == "b" * 64

    async with sessionmaker() as session:
        rows = list((await session.scalars(select(Job))).all())
    assert len(rows) == 1
    assert rows[0].correlation_id == "b" * 64
