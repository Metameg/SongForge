"""Edge-case coverage for issue #18 (observability & load test) that the happy-path
suites (`test_correlation_trace*.py`, `test_metrics_pipeline.py`, `test_web_app.py`)
don't exercise:

- `songforge.correlation.bind_correlation_id`'s own contract in isolation (minting,
  nested-ambient restore, no cross-call leakage) -- there is no dedicated unit test
  file for `correlation.py` itself.
- A job row whose `correlation_id` is `NULL` (a legacy, pre-#18 row) flowing through
  every pipeline seam that re-binds it (ingest, webhook, all four watchdog sweeps) --
  the existing suites cover this for `dispatch_claimed_job` only.
- A pipeline unit re-binding on top of a non-`None` AMBIENT correlation id (not just
  "was `None` before") still restores that exact ambient value on exit, never the
  job's own id and never `None`.
- `songforge.metrics_pipeline`'s zero-fill contract against a completely empty jobs
  table (the existing suite always seeds at least one row), a semaphore gauge
  DEcrease across two scrapes (the existing suite only proves this for the job-state
  gauge), and `/metrics`' resilience when Postgres and/or Redis are unreachable
  (asserted directly against `render_latest_with_pipeline_gauges`, plus once through
  the real HTTP route for the Redis-down case).

Per this repo's per-file stub convention, fakes are re-declared locally rather than
imported from sibling test modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator as AsyncIter

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge import correlation
from songforge.config import Settings
from songforge.jobs.generation_client import GENERATION_STATUS_COMPLETED, GenerationStatus
from songforge.jobs.ingest import ingest_claimed_job
from songforge.jobs.watchdog import (
    sweep_ingest_overdue,
    sweep_submitting_stuck,
    sweep_terminal_failures,
    sweep_waiting_overdue,
)
from songforge.metrics import REGISTRY
from songforge.metrics_pipeline import (
    refresh_job_state_gauges,
    refresh_semaphore_gauges,
    render_latest_with_pipeline_gauges,
)
from songforge.models import (
    ALL_JOB_STATES,
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
    PlaybackQueue,
)
from songforge.storage import audio_key
from songforge.web.app import create_app
from songforge.web.routes.webhook import get_notify_dependency, get_semaphore_dependency
from songforge.web.routes.webhook import get_session as webhook_get_session


# ── shared local fakes/helpers (mirrors this repo's per-file stub convention) ──────


class _FakeRedis:
    """Same shape as `tests/test_metrics_pipeline.py`'s `_FakeRedis`."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(values or {})

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def scan_iter(self, match: str) -> AsyncIter[str]:
        prefix = match[:-1]
        for key in list(self._store):
            if key.startswith(prefix):
                yield key


class _FailingRedis:
    """A Redis double whose every call raises -- exercises `/metrics`' resilience
    when Redis is unreachable."""

    async def get(self, key: str) -> str | None:
        raise RuntimeError("redis unreachable")

    async def scan_iter(self, match: str) -> AsyncIter[str]:
        raise RuntimeError("redis unreachable")
        yield ""  # pragma: no cover -- unreachable, keeps this an async generator


class _FailingSession:
    """A DB session double whose `execute` raises -- exercises `/metrics`'
    resilience when Postgres is unreachable."""

    async def execute(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("database unreachable")


def _rendered_text() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def _sample_value(text: str, name: str, labels: str) -> str | None:
    target_prefix = f"{name}{labels} "
    for line in text.splitlines():
        if line.startswith(target_prefix):
            return line[len(target_prefix) :].strip()
    return None


@pytest.fixture()
async def empty_session() -> AsyncIterator[AsyncSession]:
    """An in-memory SQLite session with the schema created but NO rows -- the
    completely-empty-database case `test_metrics_pipeline.py` never exercises (its
    fixture is always seeded before the gauges are read)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


# ── A: `correlation.bind_correlation_id` contract, in isolation ────────────────────


def test_bind_correlation_id_mints_a_fresh_id_when_value_is_none() -> None:
    """A `None` job.correlation_id (legacy row) still gets SOME id bound for the
    duration of the block, shaped like `CorrelationIdMiddleware`'s own minted id."""
    correlation.clear_correlation_id()
    with correlation.bind_correlation_id(None) as cid:
        assert cid  # non-empty
        assert len(cid) == 32  # uuid4().hex
        assert correlation.get_correlation_id() == cid
    assert correlation.get_correlation_id() is None


def test_bind_correlation_id_mints_distinct_ids_across_separate_calls() -> None:
    correlation.clear_correlation_id()
    with correlation.bind_correlation_id(None) as first:
        minted_first = first
    with correlation.bind_correlation_id(None) as second:
        minted_second = second
    assert minted_first != minted_second


def test_bind_correlation_id_restores_a_non_none_prior_ambient_on_exit() -> None:
    """The reset target is whatever was bound BEFORE, not always `None` -- a nested
    call (e.g. `receive_webhook` re-binding inside a request already carrying the
    middleware's own id) must hand back that prior id, not wipe it."""
    token = correlation.set_correlation_id("ambient-outer")
    try:
        with correlation.bind_correlation_id("job-inner"):
            assert correlation.get_correlation_id() == "job-inner"
        assert correlation.get_correlation_id() == "ambient-outer"
    finally:
        correlation.reset_correlation_id(token)


def test_sequential_top_level_binds_do_not_leak_into_each_other() -> None:
    """Guards the drain-loop scenario directly at the primitive level: one job's
    bind-then-reset must never leave a trace for the NEXT job's bind to inherit."""
    correlation.clear_correlation_id()
    with correlation.bind_correlation_id("job-a"):
        pass
    assert correlation.get_correlation_id() is None

    with correlation.bind_correlation_id("job-b") as cid_b:
        assert cid_b == "job-b"
    assert correlation.get_correlation_id() is None


# ── B: a NULL `correlation_id` job row must not crash any re-binding seam ──────────


class _FakeStorage:
    def __init__(self, existing_key: str) -> None:
        self._existing_key = existing_key

    def exists(self, key: str) -> bool:
        return key == self._existing_key

    def put(self, key: str, data: bytes) -> str:
        raise AssertionError("must not upload when the object already exists")


class _UnusedDownloader:
    async def download(self, url: str) -> bytes:
        raise AssertionError("must not download on the idempotent-exists branch")


class _UnusedGenerationClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must never call create()")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("not exercised on the idempotent-exists branch")

    async def get_status_by_id(self, task_id: str) -> None:
        raise AssertionError("not exercised on the idempotent-exists branch")


def _assign_playback_id(mapper: object, connection: object, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = 1


@pytest.fixture(autouse=True)
def _sqlite_playback_id_shim() -> Iterator[None]:
    event.listen(PlaybackQueue, "before_insert", _assign_playback_id)
    yield
    event.remove(PlaybackQueue, "before_insert", _assign_playback_id)


@pytest.fixture()
async def sqlite_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
    await engine.dispose()


async def test_ingest_with_null_correlation_id_does_not_crash(
    sqlite_session: AsyncSession,
) -> None:
    correlation.clear_correlation_id()
    key = audio_key("conv-null")
    job = Job(
        job_id="job-null-ingest",
        seq=1,
        user_id="user-1",
        prompt="p",
        state="INGEST_PENDING",
        task_id="task-null",
        conversion_id_1="conv-null",
        audio_url="http://musicgpt.test/audio/token",
        audio_duration=180.0,
        title="Untraced Legacy Song",
        ingest_attempts=0,
        available_at=datetime.now(timezone.utc),
        correlation_id=None,
    )  # type: ignore[arg-type]
    settings = Settings()

    await ingest_claimed_job(
        job,
        session=sqlite_session,
        storage=_FakeStorage(key),  # type: ignore[arg-type]
        generation_client=_UnusedGenerationClient(),  # type: ignore[arg-type]
        downloader=_UnusedDownloader(),  # type: ignore[arg-type]
        settings=settings,
    )

    assert correlation.get_correlation_id() is None


async def test_webhook_with_null_correlation_id_job_does_not_crash() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async with sessionmaker() as session:
        session.add(
            Job(
                job_id="job-null-webhook",
                seq=1,
                user_id="user-1",
                prompt="p",
                state=JOB_STATE_WAITING_FOR_WEBHOOK,
                task_id="task-null-webhook",
                conversion_id_1="conv-null-webhook",
                webhook_url="http://web:8000/api/generation/webhook",
                correlation_id=None,
            )  # type: ignore[arg-type]
        )
        await session.commit()

    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    class _NotifySpy:
        async def __call__(self, channel: str, payload: str) -> None:
            return None

    class _NoopSemaphore:
        async def release(self, user_id: str) -> None:
            return None

    app.dependency_overrides[webhook_get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    app.dependency_overrides[get_semaphore_dependency] = lambda: _NoopSemaphore()
    client = TestClient(app)

    resp = client.post(
        "/api/generation/webhook",
        json={
            "task_id": "task-null-webhook",
            "conversion_id": "conv-null-webhook",
            "conversion_path": "http://musicgpt.test/audio/token",
            "conversion_duration": 180.0,
            "title": "Untraced Song",
        },
    )

    assert resp.status_code == 200
    # The response's own X-Correlation-ID is the REQUEST's (middleware-minted) id,
    # unaffected by the job's null id -- see `webhook.py::receive_webhook`'s docstring.
    assert resp.headers["X-Correlation-ID"]

    async with sessionmaker() as session:
        row = (
            await session.scalars(select(Job).where(Job.job_id == "job-null-webhook"))
        ).one()
        assert row.state == JOB_STATE_INGEST_PENDING


class _StubGenerationStatusClient:
    def __init__(self, status: GenerationStatus) -> None:
        self._status = status

    async def get_status_by_id(self, task_id: str) -> GenerationStatus:
        return self._status


def _completed_status() -> GenerationStatus:
    return GenerationStatus(
        status=GENERATION_STATUS_COMPLETED,
        audio_url="http://musicgpt.test/audio/recovered",
        duration=181.0,
        title="Recovered",
    )


async def test_sweep_waiting_overdue_with_null_correlation_id_does_not_crash() -> None:
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-null-waiting",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_WAITING_FOR_WEBHOOK,
        task_id="task-null-waiting",
        eta=60,
        available_at=datetime.now(timezone.utc),
        correlation_id=None,
    )  # type: ignore[arg-type]

    intent = await sweep_waiting_overdue(
        job,
        client=_StubGenerationStatusClient(_completed_status()),  # type: ignore[arg-type]
        settings=Settings(),
    )

    assert intent is not None
    assert job.state == JOB_STATE_INGEST_PENDING
    assert correlation.get_correlation_id() is None


async def test_sweep_submitting_stuck_with_null_correlation_id_does_not_crash() -> None:
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-null-submitting",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_SUBMITTING,
        attempts=0,
        available_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        correlation_id=None,
    )  # type: ignore[arg-type]

    channel, payload = await sweep_submitting_stuck(job, settings=Settings())

    assert payload == job.job_id
    assert job.attempts == 1
    assert correlation.get_correlation_id() is None


async def test_sweep_ingest_overdue_with_null_correlation_id_does_not_crash() -> None:
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-null-ingest-overdue",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_INGEST_PENDING,
        ingest_attempts=0,
        available_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        correlation_id=None,
    )  # type: ignore[arg-type]

    await sweep_ingest_overdue(job, settings=Settings())

    assert job.state == JOB_STATE_INGEST_PENDING  # nudged, not escalated
    assert correlation.get_correlation_id() is None


async def test_sweep_terminal_failures_with_null_correlation_id_does_not_crash() -> None:
    correlation.clear_correlation_id()
    job = Job(
        job_id="job-null-terminal",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_FAILED,
        client_ip="203.0.113.9",
        is_authenticated=False,
        created_at=datetime.now(timezone.utc),
        failure_handled_at=None,
        correlation_id=None,
    )  # type: ignore[arg-type]

    intent = await sweep_terminal_failures(job)

    assert intent is not None
    assert job.failure_handled_at is not None
    assert correlation.get_correlation_id() is None


# ── C: re-binding on top of a NON-None ambient restores that exact ambient ─────────


async def test_ingest_restores_a_non_none_prior_ambient_after_processing(
    sqlite_session: AsyncSession,
) -> None:
    key = audio_key("conv-ambient")
    job = Job(
        job_id="job-ambient-ingest",
        seq=1,
        user_id="user-1",
        prompt="p",
        state="INGEST_PENDING",
        task_id="task-ambient",
        conversion_id_1="conv-ambient",
        audio_url="http://musicgpt.test/audio/token",
        audio_duration=180.0,
        title="Ambient Song",
        ingest_attempts=0,
        available_at=datetime.now(timezone.utc),
        correlation_id="job-own-ambient-id",
    )  # type: ignore[arg-type]
    token = correlation.set_correlation_id("ambient-caller")
    try:
        await ingest_claimed_job(
            job,
            session=sqlite_session,
            storage=_FakeStorage(key),  # type: ignore[arg-type]
            generation_client=_UnusedGenerationClient(),  # type: ignore[arg-type]
            downloader=_UnusedDownloader(),  # type: ignore[arg-type]
            settings=Settings(),
        )
        # Not `None`, and not the job's own id -- the caller's ambient binding.
        assert correlation.get_correlation_id() == "ambient-caller"
    finally:
        correlation.reset_correlation_id(token)


async def test_sweep_submitting_stuck_restores_a_non_none_prior_ambient() -> None:
    job = Job(
        job_id="job-ambient-submitting",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_SUBMITTING,
        attempts=0,
        available_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        correlation_id="job-own-ambient-submitting",
    )  # type: ignore[arg-type]
    token = correlation.set_correlation_id("ambient-caller-2")
    try:
        await sweep_submitting_stuck(job, settings=Settings())
        assert correlation.get_correlation_id() == "ambient-caller-2"
    finally:
        correlation.reset_correlation_id(token)


async def test_sweep_terminal_failures_restores_a_non_none_prior_ambient() -> None:
    job = Job(
        job_id="job-ambient-terminal",
        seq=1,
        user_id="user-1",
        prompt="p",
        state=JOB_STATE_FAILED,
        client_ip="203.0.113.10",
        is_authenticated=False,
        created_at=datetime.now(timezone.utc),
        failure_handled_at=None,
        correlation_id="job-own-ambient-terminal",
    )  # type: ignore[arg-type]
    token = correlation.set_correlation_id("ambient-caller-3")
    try:
        await sweep_terminal_failures(job)
        assert correlation.get_correlation_id() == "ambient-caller-3"
    finally:
        correlation.reset_correlation_id(token)


# ── D: pipeline gauges -- empty database + semaphore re-scrape decrease ────────────


async def test_refresh_job_state_gauges_zero_fills_every_state_on_a_completely_empty_db(
    empty_session: AsyncSession,
) -> None:
    """Distinct from `test_metrics_pipeline.py`'s zero-fill test, which always seeds
    at least one row -- proves the zero-fill loop doesn't implicitly depend on the
    `GROUP BY` returning at least one group."""
    await refresh_job_state_gauges(empty_session)

    text = _rendered_text()
    for state in ALL_JOB_STATES:
        assert (
            _sample_value(text, "songforge_jobs_in_state", f'{{state="{state}"}}')
            == "0.0"
        ), f"expected state {state!r} zero-filled on an empty table"


async def test_refresh_semaphore_gauges_second_scrape_reflects_a_decrease() -> None:
    """Mirrors `test_metrics_pipeline.py`'s point-in-time proof for the JOB-STATE
    gauge, but for the SEMAPHORE gauge -- a `.set()`, not an accumulation, so a
    dropping value must be reflected too, not just a rising one."""
    settings = Settings()
    redis = _FakeRedis({settings.semaphore_global_key: "3"})
    await refresh_semaphore_gauges(redis, settings)  # type: ignore[arg-type]
    assert (
        _sample_value(_rendered_text(), "songforge_generation_slots_in_use", '{scope="global"}')
        == "3.0"
    )

    redis._store[settings.semaphore_global_key] = "1"
    await refresh_semaphore_gauges(redis, settings)  # type: ignore[arg-type]
    assert (
        _sample_value(_rendered_text(), "songforge_generation_slots_in_use", '{scope="global"}')
        == "1.0"
    )


# ── E: `/metrics` resilience when Postgres and/or Redis are unreachable ────────────


async def test_render_latest_with_pipeline_gauges_survives_db_failure() -> None:
    settings = Settings(global_generation_concurrency=2)
    redis = _FakeRedis({settings.semaphore_global_key: "1"})

    response = await render_latest_with_pipeline_gauges(
        _FailingSession(), redis, settings  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    # Redis-backed gauges still refresh even though the DB call raised.
    assert "songforge_generation_slots_in_use" in body
    assert "songforge_http_requests_total" in body


async def test_render_latest_with_pipeline_gauges_survives_redis_failure(
    empty_session: AsyncSession,
) -> None:
    response = await render_latest_with_pipeline_gauges(
        empty_session, _FailingRedis(), Settings()  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    # DB-backed gauges still refresh even though the Redis call raised.
    assert "songforge_jobs_in_state" in body
    assert "songforge_http_requests_total" in body


async def test_render_latest_with_pipeline_gauges_survives_both_datastores_down() -> None:
    response = await render_latest_with_pipeline_gauges(
        _FailingSession(), _FailingRedis(), Settings()  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    # Neither datastore's outage may take down the response OR the static counters.
    assert "songforge_http_requests_total" in body


def test_metrics_http_route_returns_200_when_redis_is_down() -> None:
    """Same resilience proof as the two tests above, but through the REAL `/metrics`
    route (`create_app`'s injected-`redis=` seam), not just the unit-level function --
    per `web/app.py`'s own docstring, a bare `TestClient(app)` never runs `lifespan`,
    so this fake Redis is only ever touched by `metrics_route`, never a broadcaster."""
    app = create_app(redis=_FailingRedis())  # type: ignore[arg-type]
    client = TestClient(app)

    resp = client.get("/metrics", follow_redirects=False)

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "songforge_http_requests_total" in resp.text
