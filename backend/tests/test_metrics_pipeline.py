"""On-scrape pipeline gauges: jobs-by-state (Postgres) + semaphore slot utilization
(Redis) -- issue #18, acceptance criterion #2 / gap #2.

The load-driver counters in `songforge/metrics.py` (`jobs_created_total`,
`semaphore_acquire_denied_total`, etc.) are already organized by load driver, but
"how many jobs are in each state RIGHT NOW" and "how many generation slots are in use
RIGHT NOW" are point-in-time facts, not accumulating counters -- no `.inc()` call ever
fires when a job simply SITS in `WAITING_FOR_WEBHOOK`. This file drives the proposed
seam, `songforge.metrics_pipeline`, which reads Postgres/Redis fresh at every scrape
and sets `Gauge`s registered on the shared `songforge.metrics.REGISTRY` (so a single
`GET /metrics` still renders everything from one registry).

Uses an in-memory SQLite session (mirrors `tests/test_ingest.py`'s `session` fixture)
and a file-local `_FakeRedis` (mirrors `tests/test_pointer_cache.py`'s `_FakeRedis`
convention: a minimal in-memory async double, get/set + scan_iter) so both datastores
are fakeable at the unit level -- no live Postgres or Redis.

Production change that turns these green: a new `songforge/metrics_pipeline.py`
module defining `jobs_in_state`/`generation_slots_in_use`/`generation_slots_capacity`
Gauges (registered on `metrics.REGISTRY`) plus `refresh_job_state_gauges`,
`refresh_semaphore_gauges`, `refresh_pipeline_gauges`, and
`render_latest_with_pipeline_gauges` -- see this file's per-test docstrings for the
exact seam each one is expected to expose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import AsyncIterator as AsyncIter

import pytest
from prometheus_client import generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.metrics import REGISTRY
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_QUEUED,
    JOB_STATE_READY,
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
)


class _FakeRedis:
    """Same shape as `tests/test_pointer_cache.py`'s `_FakeRedis`, extended with an
    async `scan_iter` (mirrors `RedisSemaphoreBackend.scan_keys`'s real use of it) --
    kept file-local per this repo's per-file stub convention."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(values or {})

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def scan_iter(self, match: str) -> AsyncIter[str]:
        assert match.endswith("*"), f"unsupported fake scan_iter match: {match!r}"
        prefix = match[:-1]
        for key in list(self._store):
            if key.startswith(prefix):
                yield key


@pytest.fixture()
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _job(job_id: str, seq: int, state: str, **overrides: object) -> Job:
    defaults: dict[str, object] = dict(
        job_id=job_id,
        seq=seq,
        user_id="user-1",
        prompt="p",
        state=state,
    )
    defaults.update(overrides)
    return Job(**defaults)  # type: ignore[arg-type]


def _rendered_text() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def _sample_value(text: str, name: str, labels: str) -> str | None:
    """Find the value of one rendered Prometheus sample line, e.g.
    `_sample_value(text, "songforge_jobs_in_state", '{state="QUEUED"}')`."""
    target_prefix = f"{name}{labels} "
    for line in text.splitlines():
        if line.startswith(target_prefix):
            return line[len(target_prefix) :].strip()
    return None


# ── jobs-by-state gauge: read from Postgres at scrape time ────────────────────────


async def test_refresh_job_state_gauges_reflects_seeded_postgres_counts(
    session: AsyncSession,
) -> None:
    """Seeds a mixed set of job rows and asserts EVERY job state is exposed, including
    states with zero rows (a scraper must see `0`, not a missing series, to graph
    "jobs by state" without gaps -- AC2)."""
    from songforge.metrics_pipeline import refresh_job_state_gauges

    session.add_all(
        [
            _job("j1", 1, JOB_STATE_QUEUED),
            _job("j2", 2, JOB_STATE_QUEUED),
            _job("j3", 3, JOB_STATE_SUBMITTING),
            _job("j4", 4, JOB_STATE_READY),
            _job("j5", 5, JOB_STATE_READY),
            _job("j6", 6, JOB_STATE_READY),
            _job("j7", 7, JOB_STATE_FAILED),
        ]
    )
    await session.commit()

    await refresh_job_state_gauges(session)

    text = _rendered_text()
    assert _sample_value(text, "songforge_jobs_in_state", '{state="QUEUED"}') == "2.0"
    assert _sample_value(text, "songforge_jobs_in_state", '{state="SUBMITTING"}') == "1.0"
    assert _sample_value(text, "songforge_jobs_in_state", '{state="READY"}') == "3.0"
    assert _sample_value(text, "songforge_jobs_in_state", '{state="FAILED"}') == "1.0"
    # Zero-count states must still be present as an explicit `0.0` series.
    assert (
        _sample_value(text, "songforge_jobs_in_state", '{state="WAITING_FOR_WEBHOOK"}')
        == "0.0"
    )
    assert (
        _sample_value(text, "songforge_jobs_in_state", '{state="INGEST_PENDING"}')
        == "0.0"
    )


async def test_refresh_job_state_gauges_is_point_in_time_not_cumulative(
    session: AsyncSession,
) -> None:
    """Unlike a Counter, a re-scrape must reflect the CURRENT count, including a
    DECREASE -- proves this is a point-in-time gauge read, not an ever-growing sum."""
    from songforge.metrics_pipeline import refresh_job_state_gauges

    session.add_all([_job("j1", 1, JOB_STATE_QUEUED), _job("j2", 2, JOB_STATE_QUEUED)])
    await session.commit()
    await refresh_job_state_gauges(session)
    assert _sample_value(_rendered_text(), "songforge_jobs_in_state", '{state="QUEUED"}') == "2.0"

    # A job leaves QUEUED (e.g. dispatch claimed it) -- the next scrape must show 1, not 2.
    j1 = await session.get(Job, "j1")
    assert j1 is not None
    j1.state = JOB_STATE_SUBMITTING
    await session.commit()

    await refresh_job_state_gauges(session)

    text = _rendered_text()
    assert _sample_value(text, "songforge_jobs_in_state", '{state="QUEUED"}') == "1.0"
    assert _sample_value(text, "songforge_jobs_in_state", '{state="SUBMITTING"}') == "1.0"


# ── semaphore utilization gauge: read from Redis at scrape time ───────────────────


async def test_refresh_semaphore_gauges_reflects_the_global_counter_and_capacity() -> None:
    from songforge.metrics_pipeline import refresh_semaphore_gauges

    settings = Settings(global_generation_concurrency=5)
    redis = _FakeRedis({settings.semaphore_global_key: "2"})

    await refresh_semaphore_gauges(redis, settings)  # type: ignore[arg-type]

    text = _rendered_text()
    assert (
        _sample_value(text, "songforge_generation_slots_in_use", '{scope="global"}')
        == "2.0"
    )
    assert (
        _sample_value(text, "songforge_generation_slots_capacity", '{scope="global"}')
        == "5.0"
    )


async def test_refresh_semaphore_gauges_sums_every_per_user_key_into_the_user_scope() -> (
    None
):
    """`scope="user"` is a single low-cardinality series (never per-user-id, which
    would be unbounded cardinality) -- the total in-flight count across every user
    currently holding a slot."""
    from songforge.metrics_pipeline import refresh_semaphore_gauges

    settings = Settings()
    prefix = settings.semaphore_user_key_prefix
    redis = _FakeRedis(
        {
            settings.semaphore_global_key: "3",
            f"{prefix}user-a": "1",
            f"{prefix}user-b": "2",
        }
    )

    await refresh_semaphore_gauges(redis, settings)  # type: ignore[arg-type]

    text = _rendered_text()
    assert (
        _sample_value(text, "songforge_generation_slots_in_use", '{scope="user"}')
        == "3.0"
    )


async def test_refresh_semaphore_gauges_with_no_keys_set_reports_zero() -> None:
    from songforge.metrics_pipeline import refresh_semaphore_gauges

    settings = Settings()
    redis = _FakeRedis({})

    await refresh_semaphore_gauges(redis, settings)  # type: ignore[arg-type]

    text = _rendered_text()
    assert (
        _sample_value(text, "songforge_generation_slots_in_use", '{scope="global"}')
        == "0.0"
    )
    assert (
        _sample_value(text, "songforge_generation_slots_in_use", '{scope="user"}')
        == "0.0"
    )


# ── combined render: same shared registry as the existing HTTP/job-driver counters ─


async def test_render_latest_with_pipeline_gauges_shares_the_registry_with_http_metrics(
    session: AsyncSession,
) -> None:
    """Proves the pipeline gauges land on the SAME `REGISTRY` `/metrics` already
    serves (`songforge.metrics.render_latest`) -- a scraper hits one endpoint and sees
    both families, per AC1's "counters organized by load driver" + AC2's pipeline view."""
    from songforge import metrics
    from songforge.metrics_pipeline import render_latest_with_pipeline_gauges

    # Force at least one sample on an existing load-driver counter so its presence
    # below is deterministic regardless of what earlier tests in the suite did.
    metrics.http_requests_total.labels(
        method="GET", path="/metrics-pipeline-probe", status="200"
    ).inc()
    session.add(_job("j1", 1, JOB_STATE_QUEUED))
    await session.commit()
    settings = Settings(global_generation_concurrency=1)
    redis = _FakeRedis({settings.semaphore_global_key: "1"})

    response = await render_latest_with_pipeline_gauges(session, redis, settings)  # type: ignore[arg-type]

    body = response.body.decode("utf-8") if isinstance(response.body, bytes) else response.body
    assert "songforge_jobs_in_state" in body
    assert "songforge_generation_slots_in_use" in body
    # The existing load-driver counter family (metrics.py, already GREEN) must still
    # be present -- confirms this doesn't spin up a second, disconnected registry.
    assert "songforge_http_requests_total" in body
