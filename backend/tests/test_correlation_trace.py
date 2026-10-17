"""End-to-end correlation trace, part 1: submit-time ID persists on the job row and
re-binds across the dispatch step (issue #18, acceptance criterion #1 / gap #1).

A request already gets a correlation ID from `CorrelationIdMiddleware` (see
`tests/test_web_app.py`), and every log line within THAT request carries it (see
`tests/test_logging.py`). But the generation pipeline runs async, across a worker
process that picks the job up long after the original request has returned -- without
persisting the ID on the job row and re-binding it at each pipeline seam, the trace
breaks the moment dispatch (or ingest/webhook/watchdog, see
`tests/test_correlation_trace_pipeline.py`) picks the job up.

Mirrors `tests/test_create_route.py`'s (SQLite session override, `Job.seq`
`before_insert` shim) and `tests/test_dispatch.py`'s (claimed-job + fake
semaphore/client) fixtures exactly -- this file adds no new test infrastructure, only
new assertions layered on those established patterns.

Production change that turns these green: a new nullable `Job.correlation_id` column
(+ migration) in `songforge/models.py`; `web/routes/create.py` persisting it from
`songforge.correlation.get_correlation_id()`; `jobs/dispatch.py::dispatch_claimed_job`
binding `job.correlation_id` into the correlation contextvar for the duration of its
work (and resetting it afterward, mirroring `CorrelationIdMiddleware`'s own
bind/reset-in-finally shape).
"""

from __future__ import annotations

import io
import itertools
import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge import correlation
from songforge.config import Settings
from songforge.jobs.dispatch import dispatch_claimed_job
from songforge.jobs.generation_client import GenerationHandles
from songforge.logging_setup import configure_logging
from songforge.models import JOB_STATE_SUBMITTING, Base, Job
from songforge.web.app import create_app
from songforge.web.routes.create import get_notify_dependency, get_session

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


async def _job_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Job]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(Job))).all())


# ── POST /create persists the submit-time correlation ID (gap #1, part A) ─────────


async def test_create_persists_correlation_id_from_inbound_header(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client = _build_client(sessionmaker)

    resp = client.post(
        "/create",
        json={"prompt": "a song traced end to end"},
        headers={"X-Correlation-ID": "trace-submit-1"},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Correlation-ID"] == "trace-submit-1"
    rows = await _job_rows(sessionmaker)
    assert len(rows) == 1
    assert rows[0].correlation_id == "trace-submit-1"


async def test_create_persists_the_generated_correlation_id_when_header_absent(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client = _build_client(sessionmaker)

    resp = client.post("/create", json={"prompt": "another traced song"})

    assert resp.status_code == 200
    minted_id = resp.headers["X-Correlation-ID"]
    rows = await _job_rows(sessionmaker)
    assert rows[0].correlation_id == minted_id


async def test_two_submits_persist_two_distinct_correlation_ids(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Guards against a naive implementation that binds one process-wide ID instead
    of reading the request's own bound ID each time."""
    client = _build_client(sessionmaker)

    client.post(
        "/create", json={"prompt": "song A"}, headers={"X-Correlation-ID": "trace-a"}
    )
    client.post(
        "/create", json={"prompt": "song B"}, headers={"X-Correlation-ID": "trace-b"}
    )

    rows = await _job_rows(sessionmaker)
    assert {row.correlation_id for row in rows} == {"trace-a", "trace-b"}


# ── dispatch re-binds the job's correlation ID for its own log lines (gap #1, B) ──


class _RecordingSemaphore:
    async def acquire(self, user_id: str) -> bool:
        return True

    async def release(self, user_id: str) -> None:
        return None

    async def reconcile_from_active_count(self, count: int) -> None:
        return None


class _StubGenerationClient:
    def __init__(self, outcome: GenerationHandles | Exception) -> None:
        self._outcome = outcome

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


async def _never_called_counter() -> int:
    raise AssertionError("count_active_jobs must only be consulted on the 429 path")


def _claimed_job(**overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="a song about testing",
        lyrics=None,
        state=JOB_STATE_SUBMITTING,
        attempts=0,
        available_at=datetime.now(timezone.utc),
        webhook_url="http://web:8000/api/generation/webhook",
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


def _log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    text = stream.getvalue().strip()
    return [json.loads(line) for line in text.splitlines()] if text else []


async def test_dispatch_claimed_job_binds_job_correlation_id_into_log_lines() -> None:
    """Every log line dispatch emits for THIS job (e.g. `dispatch_submitted`) must
    carry the job's own submit-time correlation ID, not whatever (if anything) was
    ambient before dispatch picked the job up -- otherwise the trace breaks the moment
    a worker, not the original request, does the logging."""
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    correlation.clear_correlation_id()
    handles = GenerationHandles(
        task_id="t1",
        conversion_id_1="c1",
        conversion_id_2="c2",
        eta=60,
        credit_estimate=1.0,
    )
    job = _claimed_job(correlation_id="trace-dispatch-1")
    settings = Settings()

    await dispatch_claimed_job(
        job,
        semaphore=_RecordingSemaphore(),  # type: ignore[arg-type]
        client=_StubGenerationClient(handles),  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    dispatch_lines = [
        line for line in _log_lines(stream) if line.get("event") == "dispatch_submitted"
    ]
    assert dispatch_lines, "expected a dispatch_submitted log line"
    assert dispatch_lines[0]["correlation_id"] == "trace-dispatch-1"


async def test_dispatch_claimed_job_resets_correlation_id_after_processing() -> None:
    """The bind must be scoped to this one job -- a drain loop handles many jobs
    back-to-back in the same coroutine (`worker/dispatch.py::_drain_ready_jobs`), so a
    leaked binding would mislabel the NEXT job's log lines with THIS job's ID."""
    correlation.clear_correlation_id()
    handles = GenerationHandles(
        task_id="t1",
        conversion_id_1="c1",
        conversion_id_2="c2",
        eta=60,
        credit_estimate=1.0,
    )
    job = _claimed_job(job_id="job-2", correlation_id="trace-dispatch-2")
    settings = Settings()

    await dispatch_claimed_job(
        job,
        semaphore=_RecordingSemaphore(),  # type: ignore[arg-type]
        client=_StubGenerationClient(handles),  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert correlation.get_correlation_id() is None


async def test_dispatch_claimed_job_with_no_correlation_id_leaves_context_unset() -> None:
    """A pre-#18 row (or any row created before the column existed) has
    `correlation_id is None` -- dispatch must not crash, and must not fabricate one."""
    correlation.clear_correlation_id()
    handles = GenerationHandles(
        task_id="t1",
        conversion_id_1="c1",
        conversion_id_2="c2",
        eta=60,
        credit_estimate=1.0,
    )
    job = _claimed_job(job_id="job-3", correlation_id=None)
    settings = Settings()

    await dispatch_claimed_job(
        job,
        semaphore=_RecordingSemaphore(),  # type: ignore[arg-type]
        client=_StubGenerationClient(handles),  # type: ignore[arg-type]
        settings=settings,
        count_active_jobs=_never_called_counter,
    )

    assert correlation.get_correlation_id() is None
