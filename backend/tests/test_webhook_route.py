"""Edge tests for `POST /api/generation/webhook` (issue #13, acceptance criterion #1:
thin/fast webhook that records metadata, flips `WAITING_FOR_WEBHOOK -> INGEST_PENDING`,
NOTIFYs, and returns 200; plus idempotency and the failure-status path).

Drives the FastAPI app through real HTTP (`TestClient`) with an in-memory SQLite
session override, mirroring `tests/test_create_route.py`'s dependency-injection
pattern (`get_session`/notify-dependency overrides, the `Job.seq` `before_insert`
shim -- see that file's module docstring for why SQLite needs it).

Production change that turns these green: `songforge/web/routes/webhook.py` (router +
`get_session`/`get_notify_dependency`, registered in `web/app.py`), the new `Job`
columns (`audio_url`, `audio_duration`, `title`, `song_id`) in `models.py` + migration
`0004`, and `settings.ingest_channel` in `config.py`.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import get_settings
from songforge.jobs import generation_client
from songforge.metrics import webhooks_received_total
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
)
from songforge.web.app import create_app
from songforge.web.routes.webhook import (
    get_notify_dependency,
    get_semaphore_dependency,
    get_session,
)

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
    """Records `(channel, payload)` calls; injected via `get_notify_dependency`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, channel: str, payload: str) -> None:
        self.calls.append((channel, payload))


def _build_client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[TestClient, _NotifySpy]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    notify_spy = _NotifySpy()
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
    return TestClient(app), notify_spy


async def _insert_job(
    sessionmaker: async_sessionmaker[AsyncSession], **overrides: object
) -> None:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        user_id="user-1",
        prompt="a song about testing",
        lyrics=None,
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        eta=60,
        credit_estimate=1.0,
        webhook_url="http://web:8000/api/generation/webhook",
        audio_url=None,
        audio_duration=None,
        title=None,
        song_id=None,
    )
    defaults.update(overrides)
    job = Job(**defaults)  # type: ignore[arg-type]
    async with sessionmaker() as session:
        session.add(job)
        await session.commit()


async def _get_job(sessionmaker: async_sessionmaker[AsyncSession], job_id: str) -> Job:
    async with sessionmaker() as session:
        result = await session.scalars(select(Job).where(Job.job_id == job_id))
        return result.one()


def _webhook_body(**overrides: object) -> dict[str, object]:
    """Mirrors `simulator.schemas.WebhookPayload`'s success-path shape."""
    body: dict[str, object] = dict(
        subtype="music_ai",
        task_id="task-1",
        conversion_id="conv-1",
        conversion_path="http://musicgpt.test/audio/hint-token",
        conversion_duration=181.0,
        title="A Generated Song",
        status=None,
    )
    body.update(overrides)
    return body


# ── Success: record metadata, transition, NOTIFY, 200 (criterion #1) ──────────────


async def test_webhook_success_records_metadata_and_transitions_to_ingest_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.audio_url == "http://musicgpt.test/audio/hint-token"
    assert job.audio_duration == 181.0
    assert job.title == "A Generated Song"

    assert len(notify_spy.calls) == 1
    channel, payload = notify_spy.calls[0]
    assert channel == get_settings().ingest_channel
    assert payload == "job-1"


async def test_webhook_notifies_after_the_persisting_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Ordering IS the criterion (mirrors `test_create_route.py`'s equivalent test):
    the transition must be committed to Postgres before NOTIFY fires."""
    await _insert_job(sessionmaker)
    events: list[str] = []

    class _CommitTrackingSession:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

        async def commit(self) -> None:
            await self._session.commit()
            events.append("committed")

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield _CommitTrackingSession(session)  # type: ignore[misc]

    class _OrderedNotifySpy:
        async def __call__(self, channel: str, payload: str) -> None:
            events.append(f"notify:{channel}:{payload}")

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _OrderedNotifySpy()
    client = TestClient(app)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert events[0] == "committed"
    assert events[1].startswith("notify:")


async def test_webhook_lookup_uses_a_row_lock_for_update(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """LOW/MED finding: two genuinely concurrent webhook deliveries for the same
    `task_id` must not both observe WAITING_FOR_WEBHOOK and both transition + NOTIFY
    -- `with_for_update()` on the lookup closes that race at its source (mirrors
    `jobs.dispatch.claim_next_job`'s own row lock). True concurrent contention isn't
    reproducible against an in-memory SQLite session (SQLite has no row-level
    locking and silently no-ops `with_for_update()`, exactly like dispatch's
    precedent), so this instead captures the actual `Select` statement the route
    passes to `session.scalars(...)` and proves it would compile to a real
    `SELECT ... FOR UPDATE` against Postgres."""
    await _insert_job(sessionmaker)
    captured: list[Any] = []

    class _CapturingSession:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

        async def scalars(self, stmt: Any, *args: object, **kwargs: object) -> Any:
            captured.append(stmt)
            return await self._session.scalars(stmt, *args, **kwargs)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield _CapturingSession(session)  # type: ignore[misc]

    notify_spy = _NotifySpy()
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
    client = TestClient(app)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert captured, "the job lookup must go through session.scalars"
    from sqlalchemy.dialects import postgresql

    compiled = str(captured[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled.upper()


async def test_webhook_returns_200_even_if_notify_raises(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """LOW finding: `notify(...)` fires after the state transition is already
    durably committed -- a NOTIFY failure (e.g. a dropped connection between commit
    and this call) must not surface as a 500 to the external generation API's
    webhook caller, which would incorrectly signal failure and invite a retry the
    idempotency check would then just no-op anyway. `_safe_notify` swallows it."""
    await _insert_job(sessionmaker)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    class _ExplodingNotify:
        async def __call__(self, channel: str, payload: str) -> None:
            raise RuntimeError("notify boom")

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _ExplodingNotify()
    client = TestClient(app)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_INGEST_PENDING  # already committed before notify ran


async def test_webhook_never_calls_the_generation_client(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thin & fast (PRD #6): the webhook route only records metadata + flips state +
    NOTIFYs. The audio download/upload is the async ingest worker's job entirely.
    Mirrors `test_create_route.py`'s equivalent generation-client boundary guard."""

    async def _explode(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the webhook route must never call the generation client")

    monkeypatch.setattr(
        generation_client.HttpGenerationClient, "create", _explode, raising=True
    )
    monkeypatch.setattr(
        generation_client.HttpGenerationClient,
        "get_audio_url_by_id",
        _explode,
        raising=True,
    )
    await _insert_job(sessionmaker)
    client, _ = _build_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200


# ── Idempotency: duplicate/late webhooks are a no-op (criterion, PRD "Idempotency") ─


@pytest.mark.parametrize(
    "state", [JOB_STATE_INGEST_PENDING, JOB_STATE_READY, JOB_STATE_FAILED]
)
async def test_webhook_is_a_noop_for_a_job_already_past_waiting_for_webhook(
    sessionmaker: async_sessionmaker[AsyncSession], state: str
) -> None:
    await _insert_job(sessionmaker, state=state)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == state  # unchanged -- no re-transition
    assert notify_spy.calls == []  # no double-NOTIFY


async def test_duplicate_webhook_after_a_successful_one_does_not_re_notify(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)

    first = client.post("/api/generation/webhook", json=_webhook_body())
    second = client.post("/api/generation/webhook", json=_webhook_body())

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(notify_spy.calls) == 1  # only the first transition notifies
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_INGEST_PENDING


async def test_webhook_for_an_unknown_task_id_is_a_noop_200() -> None:
    """Design choice (documented per the phase-1 instructions): an unrecognized
    `task_id` -- a stray/replayed webhook, or a race with replication lag -- is a
    200 no-op, NOT a 404. A non-2xx here would only invite the external API to
    retry something this service can never act on; a no-op 200 is safe and final."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post(
        "/api/generation/webhook", json=_webhook_body(task_id="no-such-task")
    )

    assert resp.status_code == 200
    assert notify_spy.calls == []


# ── Failure-status webhook -> FAILED (criterion; refund/notify are OUT of scope) ──


@pytest.mark.parametrize("status_value", ["ERROR", "FAILED"])
async def test_webhook_failure_status_marks_the_job_failed(
    sessionmaker: async_sessionmaker[AsyncSession], status_value: str
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status=status_value),
    )

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_FAILED
    # Quota refund + user SSE notification on failure are a later ticket (see
    # .orchestrator/CONTEXT.md scope) -- not built here, and no NOTIFY is expected:
    # there is no ingest work to wake a worker for on a failed generation.
    assert notify_spy.calls == []


async def test_webhook_failure_status_is_idempotent_too(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker, state=JOB_STATE_FAILED)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_FAILED
    assert notify_spy.calls == []


# ── Malformed / missing-field payloads -> 422 (phase-4 hardening) ─────────────────
#
# FastAPI/pydantic reject these before `receive_webhook` ever runs -- proving the
# route's declared `WebhookPayload` model actually enforces its required fields
# rather than silently defaulting/coercing them, which would otherwise let a
# malformed delivery slip through as a false "success" or an unhandled crash.


async def test_webhook_missing_task_id_returns_422(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)
    body = _webhook_body()
    del body["task_id"]

    resp = client.post("/api/generation/webhook", json=body)

    assert resp.status_code == 422
    assert notify_spy.calls == []
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_WAITING_FOR_WEBHOOK  # untouched


async def test_webhook_missing_conversion_id_returns_422(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)
    body = _webhook_body()
    del body["conversion_id"]

    resp = client.post("/api/generation/webhook", json=body)

    assert resp.status_code == 422
    assert notify_spy.calls == []


async def test_webhook_non_numeric_conversion_duration_returns_422(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_duration="not-a-number"),
    )

    assert resp.status_code == 422
    assert notify_spy.calls == []
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_WAITING_FOR_WEBHOOK  # untouched


async def test_webhook_empty_body_returns_422_not_500() -> None:
    """No datastore access should even be attempted for a body this malformed --
    validation must fail before the `get_session`/`get_notify_dependency`
    dependencies are resolved."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    client, notify_spy = _build_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json={})

    assert resp.status_code == 422
    assert notify_spy.calls == []


# ── Counters: `webhooks_received_total` increments by outcome (phase-4 hardening) ──


def _outcome_count(outcome: str) -> float:
    return webhooks_received_total.labels(outcome=outcome)._value.get()


async def test_webhook_success_increments_ingest_pending_counter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, _ = _build_client(sessionmaker)
    before = _outcome_count("ingest_pending")

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert _outcome_count("ingest_pending") == before + 1


async def test_webhook_duplicate_increments_duplicate_ignored_counter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker, state=JOB_STATE_INGEST_PENDING)
    client, _ = _build_client(sessionmaker)
    before = _outcome_count("duplicate_ignored")

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert _outcome_count("duplicate_ignored") == before + 1


async def test_webhook_failure_status_increments_failed_counter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker)
    client, _ = _build_client(sessionmaker)
    before = _outcome_count("failed")

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    assert _outcome_count("failed") == before + 1


# ── Generation semaphore release (issue #16 slot-leak fix) ─────────────────────────
#
# The webhook's failure branch (`body.status is not None or body.conversion_path is
# None` -> FAILED) is a second event-driven release site: the job leaves
# WAITING_FOR_WEBHOOK (an active state) without ever reaching ingest, so the slot
# `jobs.dispatch.dispatch_claimed_job` acquired before the generation API call must be
# released here, post-commit, best-effort. The success branch (-> INGEST_PENDING) must
# NOT release -- the job is still active; `worker/ingest.py`'s own release site handles
# that job once IT reaches READY/FAILED.


class _SemaphoreSpy:
    """File-local fake ``Semaphore``; injected via ``get_semaphore_dependency``."""

    def __init__(self) -> None:
        self.released_with: list[str] = []

    async def acquire(self, user_id: str) -> bool:
        raise AssertionError("the webhook route must never acquire a slot")

    async def release(self, user_id: str) -> None:
        self.released_with.append(user_id)

    async def reconcile_from_active_count(self, count: int) -> None:
        raise AssertionError("the webhook route must never reconcile the global cap")


def _build_client_with_semaphore(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[TestClient, _NotifySpy, _SemaphoreSpy]:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    notify_spy = _NotifySpy()
    semaphore_spy = _SemaphoreSpy()
    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: notify_spy
    app.dependency_overrides[get_semaphore_dependency] = lambda: semaphore_spy
    return TestClient(app), notify_spy, semaphore_spy


async def test_webhook_failure_releases_the_semaphore_slot(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker, user_id="user-42")
    client, _, semaphore_spy = _build_client_with_semaphore(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    assert semaphore_spy.released_with == ["user-42"]


async def test_webhook_success_does_not_release_the_semaphore_slot(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_job(sessionmaker, user_id="user-42")
    client, _, semaphore_spy = _build_client_with_semaphore(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_webhook_body())

    assert resp.status_code == 200
    assert semaphore_spy.released_with == []


async def test_webhook_noop_paths_do_not_release_the_semaphore_slot(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The idempotency no-op (already past WAITING_FOR_WEBHOOK) and the unknown-
    task-id no-op must not release either -- neither one is this route's original
    transition into FAILED."""
    await _insert_job(sessionmaker, user_id="user-42", state=JOB_STATE_FAILED)
    client, _, semaphore_spy = _build_client_with_semaphore(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    assert semaphore_spy.released_with == []


async def test_webhook_releases_the_semaphore_after_the_persisting_commit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Ordering IS the criterion (mirrors `test_webhook_notifies_after_the_
    persisting_commit`): the FAILED transition must be committed to Postgres before
    the semaphore release fires."""
    await _insert_job(sessionmaker, user_id="user-42")
    events: list[str] = []

    class _CommitTrackingSession:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        def __getattr__(self, name: str) -> Any:
            return getattr(self._session, name)

        async def commit(self) -> None:
            await self._session.commit()
            events.append("committed")

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield _CommitTrackingSession(session)  # type: ignore[misc]

    class _OrderedSemaphoreSpy:
        async def acquire(self, user_id: str) -> bool:
            raise AssertionError("must never acquire")

        async def release(self, user_id: str) -> None:
            events.append(f"release:{user_id}")

        async def reconcile_from_active_count(self, count: int) -> None:
            raise AssertionError("must never reconcile")

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    app.dependency_overrides[get_semaphore_dependency] = lambda: _OrderedSemaphoreSpy()
    client = TestClient(app)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    assert events == ["committed", "release:user-42"]


async def test_webhook_returns_200_even_if_semaphore_release_raises(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Best-effort: a raising `semaphore.release` (e.g. a Redis blip) must be
    swallowed, mirroring `_safe_notify`'s convention -- the already-committed FAILED
    transition must not be undone or surfaced as a 500."""
    await _insert_job(sessionmaker, user_id="user-42")

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    class _ExplodingSemaphore:
        async def acquire(self, user_id: str) -> bool:
            raise AssertionError("must never acquire")

        async def release(self, user_id: str) -> None:
            raise RuntimeError("redis blip during release")

        async def reconcile_from_active_count(self, count: int) -> None:
            raise AssertionError("must never reconcile")

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    app.dependency_overrides[get_semaphore_dependency] = lambda: _ExplodingSemaphore()
    client = TestClient(app)

    resp = client.post(
        "/api/generation/webhook",
        json=_webhook_body(conversion_path=None, status="ERROR"),
    )

    assert resp.status_code == 200
    job = await _get_job(sessionmaker, "job-1")
    assert job.state == JOB_STATE_FAILED  # already committed before release ran


async def test_webhook_unknown_task_increments_unknown_task_counter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client, _ = _build_client(sessionmaker)
    before = _outcome_count("unknown_task")

    resp = client.post(
        "/api/generation/webhook", json=_webhook_body(task_id="no-such-task")
    )

    assert resp.status_code == 200
    assert _outcome_count("unknown_task") == before + 1
