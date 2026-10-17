"""Contract audit for the per-user SSE events (issue #37).

Every other #37 test either hand-writes a Redis message (`test_events_sse.py`) or
hand-builds a fake `Job` and a spy publisher (`test_worker_dispatch.py`,
`test_worker_ingest.py`, `test_webhook_route.py`) -- each side owns its own idea of
the wire shape. These tests close that gap by running REAL jobs (in-memory SQLite)
through the REAL publish sites (`_drain_ready_jobs`, `receive_webhook`,
`_drain_ingest_pending`) with the REAL publisher (`redis_user_event_publisher`) over a
recording Redis double, then feeding exactly what was published through the REAL
`UserEventBroadcaster` relay and `/events`'s `_format_user_event`.

Only external services are faked: Redis, the generation API, object storage and the
audio downloader.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings, get_settings
from songforge.jobs.generation_client import GenerationHandles
from songforge.models import (
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_QUEUED,
    JOB_STATE_READY,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
    PlaybackQueue,
    Song,
)
from songforge.radio.user_events import (
    UserEventBroadcaster,
    failed_message,
    progress_message,
    ready_message,
    redis_user_event_publisher,
    user_channel,
)
from songforge.web.app import create_app
from songforge.web.routes.events import _format_user_event
from songforge.web.routes.webhook import (
    get_notify_dependency,
    get_publish_user_event_dependency,
    get_semaphore_dependency,
    get_session,
)
from songforge.worker.dispatch import _drain_ready_jobs
from songforge.worker.ingest import _drain_ingest_pending

PREFIX = get_settings().user_events_channel_prefix
AUDIO_URL = "http://musicgpt.test/audio/hint-token"

_counter = itertools.count(1)


def _assign_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_counter)


def _assign_queue_id(mapper: Any, connection: Any, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_counter)


@pytest.fixture(autouse=True)
def _sqlite_shims() -> Iterator[None]:
    """SQLite can't server-generate `Job.seq` / `PlaybackQueue.id` (Postgres
    `Identity`); same shims as `test_create_route.py` / `test_ingest.py`."""
    event.listen(Job, "before_insert", _assign_seq)
    event.listen(PlaybackQueue, "before_insert", _assign_queue_id)
    yield
    event.remove(Job, "before_insert", _assign_seq)
    event.remove(PlaybackQueue, "before_insert", _assign_queue_id)


@pytest.fixture()
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _RecordingRedis:
    """Redis double for the PUBLISH side: records `(channel, data)` exactly as the real
    publisher called it."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


class _ReplayPubSub:
    """Delivers pre-recorded publishes as real-shaped `pmessage` frames."""

    def __init__(self, published: list[tuple[str, str]], pattern: str) -> None:
        self._frames = [
            {"type": "pmessage", "pattern": pattern, "channel": c, "data": d}
            for c, d in published
            if fnmatch.fnmatchcase(c, pattern)
        ]

    async def psubscribe(self, pattern: str) -> None:
        return None

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict[str, Any] | None:
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)
        return None

    async def close(self) -> None:
        return None


class _ReplayRedis:
    def __init__(self, published: list[tuple[str, str]]) -> None:
        self._published = published

    def pubsub(self) -> _ReplayPubSub:
        pattern = f"{PREFIX}*:events"
        return _ReplayPubSub(self._published, pattern)


async def _relay_for(
    published: list[tuple[str, str]], user_id: str, count: int
) -> list[dict[str, Any]]:
    """Run the REAL broadcaster relay over `published` and collect what lands on
    `user_id`'s queue."""
    broadcaster = UserEventBroadcaster(
        redis=_ReplayRedis(published),  # type: ignore[arg-type]
        channel_prefix=PREFIX,
    )
    queue = broadcaster.register(user_id)
    await broadcaster.start()
    try:
        return [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(count)]
    finally:
        await broadcaster.stop()


def _frame(sse: str) -> tuple[str, dict[str, Any]]:
    """Parse one `_format_event` frame back into `(event, data)`."""
    lines = sse.strip().split("\n")
    assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


# ── Fakes for the generation API / storage / downloader (external services) ───────


class _Semaphore:
    async def acquire(self, user_id: str) -> bool:
        return True

    async def release(self, user_id: str) -> None:
        return None

    async def reconcile_from_active_count(self, count: int) -> None:
        return None


class _GenClient:
    def __init__(self, eta: int) -> None:
        self._eta = eta

    async def create(
        self, *, prompt: str, lyrics: str | None, webhook_url: str
    ) -> GenerationHandles:
        return GenerationHandles(
            task_id="task-1",
            conversion_id_1="conv-1",
            conversion_id_2="conv-2",
            eta=self._eta,
            credit_estimate=1.0,
        )

    async def get_audio_url_by_id(self, task_id: str) -> str:
        return AUDIO_URL


class _Storage:
    def exists(self, key: str) -> bool:
        return False

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        return f"https://cdn.test/{key}"


class _Downloader:
    async def download(self, url: str) -> bytes:
        return b"audio-bytes"


def _settings() -> Settings:
    return Settings(
        _env={
            "DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/songforge",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "http://127.0.0.1:1",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "test",
        }
    )


async def _insert_queued_job(
    sessionmaker: async_sessionmaker[AsyncSession], *, user_id: str
) -> None:
    async with sessionmaker() as session:
        session.add(
            Job(
                job_id="job-1",
                user_id=user_id,
                prompt="a song about testing",
                lyrics=None,
                state=JOB_STATE_QUEUED,
                webhook_url="http://web:8000/api/generation/webhook",
            )
        )
        await session.commit()


async def _job(sessionmaker: async_sessionmaker[AsyncSession]) -> Job:
    async with sessionmaker() as session:
        return (await session.scalars(select(Job).where(Job.job_id == "job-1"))).one()


async def _noop_notify(job_id: str) -> None:
    return None


async def _noop_pg_notify(channel: str, payload: str) -> None:
    return None


async def _run_dispatch(
    sessionmaker: async_sessionmaker[AsyncSession], redis: _RecordingRedis, eta: int
) -> None:
    await _drain_ready_jobs(
        sessionmaker,
        _Semaphore(),  # type: ignore[arg-type]
        _GenClient(eta),  # type: ignore[arg-type]
        _settings(),
        _noop_notify,
        redis_user_event_publisher(redis, PREFIX),  # type: ignore[arg-type]
    )


def _post_webhook(
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: _RecordingRedis,
    *,
    title: str | None,
) -> None:
    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _noop_pg_notify
    app.dependency_overrides[get_semaphore_dependency] = lambda: _Semaphore()
    app.dependency_overrides[get_publish_user_event_dependency] = (
        lambda: redis_user_event_publisher(redis, PREFIX)  # type: ignore[arg-type]
    )
    resp = TestClient(app).post(
        "/api/generation/webhook",
        json=dict(
            subtype="music_ai",
            task_id="task-1",
            conversion_id="conv-1",
            conversion_path=AUDIO_URL,
            conversion_duration=181.0,
            title=title,
            status=None,
        ),
    )
    assert resp.status_code == 200


async def _run_ingest(
    sessionmaker: async_sessionmaker[AsyncSession], redis: _RecordingRedis
) -> None:
    await _drain_ingest_pending(
        sessionmaker,
        _Storage(),  # type: ignore[arg-type]
        _GenClient(0),  # type: ignore[arg-type]
        _Downloader(),  # type: ignore[arg-type]
        _settings(),
        publish_user_event=redis_user_event_publisher(redis, PREFIX),  # type: ignore[arg-type]
    )


# ── 1. Builder output == what /events consumes and re-emits ───────────────────────


@pytest.mark.parametrize(
    ("message", "expected_name", "expected_data"),
    [
        (failed_message("j1"), "job-failed", {"job_id": "j1"}),
        (
            progress_message("j1", "WAITING_FOR_WEBHOOK", 90),
            "job-progress",
            {"job_id": "j1", "state": "WAITING_FOR_WEBHOOK", "eta": 90},
        ),
        (
            progress_message("j1", "INGEST_PENDING", None),
            "job-progress",
            {"job_id": "j1", "state": "INGEST_PENDING", "eta": None},
        ),
        (
            ready_message("j1", "s1", "My Song"),
            "job-ready",
            {"job_id": "j1", "song_id": "s1", "title": "My Song"},
        ),
    ],
)
async def test_builder_output_round_trips_through_relay_to_exact_sse_frame(
    message: str, expected_name: str, expected_data: dict[str, Any]
) -> None:
    channel = user_channel(PREFIX, "user-1")
    [relayed] = await _relay_for([(channel, message)], "user-1", 1)

    assert _frame(_format_user_event(relayed)) == (expected_name, expected_data)


@pytest.mark.parametrize(
    "message",
    [
        failed_message("j1"),
        progress_message("j1", "WAITING_FOR_WEBHOOK", 5),
        ready_message("j1", "s1", "T"),
    ],
)
def test_every_builder_message_carries_an_event_the_sse_layer_recognises(
    message: str,
) -> None:
    from songforge.web.routes.events import _USER_EVENT_FIELDS

    assert json.loads(message)["event"] in _USER_EVENT_FIELDS


@pytest.mark.parametrize(
    "message",
    [
        progress_message("j1", "WAITING_FOR_WEBHOOK", 5),
        ready_message("j1", "s1", "T"),
        failed_message("j1"),
    ],
)
def test_sse_frame_fields_are_a_subset_of_what_the_builder_emits(message: str) -> None:
    """Every whitelisted field the SSE layer reads exists in the builder payload (no
    field read downstream that the producer never writes)."""
    payload = json.loads(message)
    from songforge.web.routes.events import _USER_EVENT_FIELDS

    assert set(_USER_EVENT_FIELDS[payload["event"]]) <= set(payload)


def test_sse_layer_drops_extra_keys_a_producer_might_add() -> None:
    message = json.loads(ready_message("j1", "s1", "T"))
    message["user_id"] = "creator-secret"

    _name, data = _frame(_format_user_event(message))

    assert "user_id" not in data


# ── 2. eta is the real Job.eta; state/job_id always present ───────────────────────


async def test_dispatch_progress_eta_is_the_eta_the_generation_api_returned(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    redis = _RecordingRedis()

    await _run_dispatch(sessionmaker, redis, eta=137)

    [(_channel, message)] = redis.published
    assert json.loads(message)["eta"] == 137


async def test_dispatch_progress_eta_equals_the_persisted_job_eta(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    redis = _RecordingRedis()

    await _run_dispatch(sessionmaker, redis, eta=137)

    [(_channel, message)] = redis.published
    assert json.loads(message)["eta"] == (await _job(sessionmaker)).eta


async def test_dispatch_progress_always_carries_job_id_and_state(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    redis = _RecordingRedis()

    await _run_dispatch(sessionmaker, redis, eta=10)

    [(_channel, message)] = redis.published
    payload = json.loads(message)
    assert (payload["job_id"], payload["state"]) == ("job-1", JOB_STATE_WAITING_FOR_WEBHOOK)


async def test_webhook_progress_eta_is_the_eta_dispatch_persisted(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    dispatch_redis = _RecordingRedis()
    await _run_dispatch(sessionmaker, dispatch_redis, eta=137)
    webhook_redis = _RecordingRedis()

    _post_webhook(sessionmaker, webhook_redis, title="T")

    [(_channel, message)] = webhook_redis.published
    assert json.loads(message)["eta"] == 137


async def test_webhook_progress_state_is_ingest_pending(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    await _run_dispatch(sessionmaker, _RecordingRedis(), eta=1)
    webhook_redis = _RecordingRedis()

    _post_webhook(sessionmaker, webhook_redis, title="T")

    [(_channel, message)] = webhook_redis.published
    assert json.loads(message)["state"] == JOB_STATE_INGEST_PENDING


# ── 3. job-ready title == the Song row's title ────────────────────────────────────


async def _full_pipeline(
    sessionmaker: async_sessionmaker[AsyncSession], *, title: str | None
) -> _RecordingRedis:
    await _insert_queued_job(sessionmaker, user_id="user-1")
    redis = _RecordingRedis()
    await _run_dispatch(sessionmaker, redis, eta=60)
    _post_webhook(sessionmaker, redis, title=title)
    await _run_ingest(sessionmaker, redis)
    return redis


async def _ready_payload(redis: _RecordingRedis) -> dict[str, Any]:
    ready = [json.loads(m) for _c, m in redis.published if json.loads(m)["event"] == "job-ready"]
    assert len(ready) == 1
    return ready[0]


async def test_job_ready_title_equals_the_persisted_song_title(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title="A Generated Song")

    payload = await _ready_payload(redis)
    async with sessionmaker() as session:
        song = (await session.scalars(select(Song).where(Song.id == payload["song_id"]))).one()
    assert payload["title"] == song.title


async def test_job_ready_title_is_untitled_when_the_webhook_carried_no_title(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title=None)

    payload = await _ready_payload(redis)
    async with sessionmaker() as session:
        song = (await session.scalars(select(Song).where(Song.id == payload["song_id"]))).one()
    assert (payload["title"], song.title) == ("Untitled", "Untitled")


async def test_job_ready_song_id_equals_the_jobs_song_id(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title="T")

    job = await _job(sessionmaker)
    assert job.state == JOB_STATE_READY
    assert (await _ready_payload(redis))["song_id"] == job.song_id


# ── 4. Publish sites use the job's user_id and the user's own channel ─────────────


async def test_every_publish_in_the_real_pipeline_targets_the_jobs_own_channel(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title="T")

    assert {c for c, _m in redis.published} == {user_channel(PREFIX, "user-1")}


async def test_real_pipeline_publishes_progress_progress_ready_in_order(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title="T")

    assert [json.loads(m)["event"] for _c, m in redis.published] == [
        "job-progress",
        "job-progress",
        "job-ready",
    ]


async def test_real_pipeline_frames_reach_only_the_originating_identity(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end privacy contract: replay what the real pipeline published through
    the real relay -- the owner gets three frames, another identity's queue none."""
    redis = await _full_pipeline(sessionmaker, title="T")

    [other] = await asyncio.gather(_relay_expect_nothing(redis.published, "someone-else"))
    owner = await _relay_for(redis.published, "user-1", 3)

    assert (len(owner), other) == (3, [])


async def _relay_expect_nothing(
    published: list[tuple[str, str]], user_id: str
) -> list[dict[str, Any]]:
    broadcaster = UserEventBroadcaster(
        redis=_ReplayRedis(published),  # type: ignore[arg-type]
        channel_prefix=PREFIX,
    )
    queue = broadcaster.register(user_id)
    await broadcaster.start()
    try:
        await asyncio.sleep(0.2)
        return [queue.get_nowait() for _ in range(queue.qsize())]
    finally:
        await broadcaster.stop()


async def test_real_pipeline_sse_frames_match_the_documented_wire_shapes(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    redis = await _full_pipeline(sessionmaker, title="T")

    relayed = await _relay_for(redis.published, "user-1", 3)

    assert [(n, sorted(d)) for n, d in (_frame(_format_user_event(m)) for m in relayed)] == [
        ("job-progress", ["eta", "job_id", "state"]),
        ("job-progress", ["eta", "job_id", "state"]),
        ("job-ready", ["job_id", "song_id", "title"]),
    ]


def test_default_webhook_publisher_wiring_uses_the_configured_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _RecordingRedis()
    monkeypatch.setattr("songforge.web.routes.webhook.get_redis", lambda: redis)
    publish = get_publish_user_event_dependency()

    asyncio.run(publish("user-9", "{}"))

    assert redis.published == [(user_channel(PREFIX, "user-9"), "{}")]
