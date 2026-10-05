"""Edge tests for GET /queue (issue #38: queue depth + the caller's own position).

Drives the FastAPI app through real HTTP (`TestClient`) with an in-memory SQLite session
override on the injectable `songforge.web.routes.queue.get_session` dependency, mirroring
`tests/test_create_route.py`.

`PlaybackQueue.id` and `Job.seq` are Postgres `Identity()` columns SQLite cannot
auto-generate (see `test_create_route.py`'s module docstring), so the fixtures below set
`PlaybackQueue.id` EXPLICITLY (it is the FIFO order key under test) and a TEST-ONLY
`before_insert` shim fills `Job.seq`. Genuine Identity-seq ordering is covered separately
against real Postgres in `tests/test_queue_integration.py`.

Contract pinned here: `depth` counts only `played_at IS NULL` rows (including rows with
`job_id IS NULL`); `positions` are the CALLER's own queued jobs with their 1-based GLOBAL
position among the waiting rows ordered by `playback_queue.id`; `GET /queue` only READS
identity and never sets a cookie (unlike `/quota`).
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import get_settings
from songforge.models import JOB_STATE_QUEUED, Base, Job, PlaybackQueue, Song
from songforge.web.app import create_app
from songforge.web.identity import mint, sign
from songforge.web.routes.queue import get_session

_seq_counter = itertools.count(1)


def _assign_test_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq_counter)


@pytest.fixture(autouse=True)
def _sqlite_seq_shim() -> Iterator[None]:
    """SQLite can't server-generate `Job.seq` (Postgres `Identity`); fill it for this
    file's tests only."""
    event.listen(Job, "before_insert", _assign_test_seq)
    yield
    event.remove(Job, "before_insert", _assign_test_seq)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _build_client(sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    return TestClient(app)


def _with_cookie(client: TestClient, user_id: str) -> TestClient:
    client.cookies.set(
        get_settings().identity_cookie_name,
        sign(user_id, secret=get_settings().session_secret),
    )
    return client


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    rows: list[tuple[int, str | None, str | None, bool]],
) -> None:
    """Insert `(queue_id, job_id, owner_user_id, played)` rows.

    A job (owned by `owner_user_id`) is created for each non-null `job_id`; a null
    `job_id` seeds a queue row belonging to no caller.
    """
    async with sessionmaker() as session:
        for queue_id, job_id, owner, played in rows:
            song_id = f"song-{queue_id}"
            session.add(
                Song(
                    id=song_id,
                    title=f"t{queue_id}",
                    source="generated",
                    object_key=f"audio/{song_id}.mp3",
                )
            )
            if job_id is not None:
                session.add(
                    Job(
                        job_id=job_id,
                        user_id=owner or "nobody",
                        prompt="p",
                        state=JOB_STATE_QUEUED,
                        attempts=0,
                    )
                )
        await session.flush()
        for queue_id, job_id, _owner, played in rows:
            session.add(
                PlaybackQueue(
                    id=queue_id,
                    song_id=f"song-{queue_id}",
                    job_id=job_id,
                    played_at=datetime.now(timezone.utc) if played else None,
                )
            )
        await session.commit()


async def test_empty_queue_returns_zero_depth_and_no_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client = _build_client(sessionmaker)

    resp = client.get("/queue")

    assert resp.status_code == 200
    assert resp.json() == {"depth": 0, "positions": []}


async def test_response_shape_has_depth_and_job_id_position_pairs(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = mint()
    await _seed(sessionmaker, [(1, "job-a", me, False)])
    client = _with_cookie(_build_client(sessionmaker), me)

    resp = client.get("/queue")

    assert resp.status_code == 200
    assert resp.json() == {"depth": 1, "positions": [{"job_id": "job-a", "position": 1}]}


async def test_depth_counts_only_unplayed_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other = mint()
    await _seed(
        sessionmaker,
        [
            (1, "job-played", other, True),  # aired already -> excluded
            (2, "job-w1", other, False),
            (3, "job-w2", other, False),
        ],
    )
    client = _build_client(sessionmaker)

    resp = client.get("/queue")

    assert resp.json()["depth"] == 2


async def test_positions_are_one_based_global_fifo_by_queue_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me, other = mint(), mint()
    # Inserted out of id order on purpose: position must follow playback_queue.id.
    await _seed(
        sessionmaker,
        [
            (30, "job-me-late", me, False),
            (10, "job-other-1", other, False),
            (20, "job-me-early", me, False),
            (40, "job-other-2", other, False),
        ],
    )
    client = _with_cookie(_build_client(sessionmaker), me)

    body = client.get("/queue").json()

    assert body["depth"] == 4
    by_job = {p["job_id"]: p["position"] for p in body["positions"]}
    # Waiting order by id: 10(other) 20(me) 30(me) 40(other) -> global positions 2 and 3.
    assert by_job == {"job-me-early": 2, "job-me-late": 3}
    assert len(body["positions"]) == 2


async def test_played_rows_do_not_shift_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me, other = mint(), mint()
    await _seed(
        sessionmaker,
        [
            (1, "job-aired", other, True),  # now-playing / already aired: not waiting
            (2, "job-me", me, False),
        ],
    )
    client = _with_cookie(_build_client(sessionmaker), me)

    body = client.get("/queue").json()

    assert body == {"depth": 1, "positions": [{"job_id": "job-me", "position": 1}]}


async def test_other_identities_jobs_are_excluded_from_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me, other = mint(), mint()
    await _seed(
        sessionmaker,
        [(1, "job-other", other, False), (2, "job-me", me, False)],
    )
    client = _with_cookie(_build_client(sessionmaker), me)

    body = client.get("/queue").json()

    assert body["depth"] == 2
    assert [p["job_id"] for p in body["positions"]] == ["job-me"]


async def test_null_job_id_row_counts_in_depth_but_never_in_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    me = mint()
    await _seed(
        sessionmaker,
        [(1, None, None, False), (2, "job-me", me, False)],
    )
    client = _with_cookie(_build_client(sessionmaker), me)

    body = client.get("/queue").json()

    assert body["depth"] == 2
    # The seeded row still occupies global slot #1, so the caller is #2.
    assert body["positions"] == [{"job_id": "job-me", "position": 2}]


async def test_no_cookie_gets_depth_but_empty_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(sessionmaker, [(1, "job-x", mint(), False)])
    client = _build_client(sessionmaker)

    body = client.get("/queue").json()

    assert body == {"depth": 1, "positions": []}


async def test_caller_with_cookie_but_no_queued_jobs_gets_empty_positions(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(sessionmaker, [(1, "job-x", mint(), False)])
    client = _with_cookie(_build_client(sessionmaker), mint())

    body = client.get("/queue").json()

    assert body == {"depth": 1, "positions": []}


async def test_get_queue_never_sets_an_identity_cookie(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    client = _build_client(sessionmaker)

    resp = client.get("/queue")

    assert resp.status_code == 200
    assert "set-cookie" not in resp.headers
    assert get_settings().identity_cookie_name not in resp.cookies
