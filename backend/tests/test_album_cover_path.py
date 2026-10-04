"""`album_cover_path` threading contract (issue #36, RED phase).

The field travels simulator -> webhook -> Job -> ingest -> Song -> PointerRecord ->
`/now-playing` + `song-change`. Each hop is covered at its public seam; the now-playing
body lives in `test_now_playing.py`, the SSE key set in `test_events_sse.py`.

Symbols/columns that do not exist yet (`Job.album_cover_path`, `Song.album_cover_path`,
`NowPlayingView.album_cover_path`, ...) are touched only inside each test so a missing
symbol fails only that test, never collection.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.config import Settings
from songforge.models import (
    JOB_STATE_FAILED,
    JOB_STATE_INGEST_PENDING,
    JOB_STATE_READY,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    Base,
    Job,
    PlaybackQueue,
    Song,
)
from songforge.simulator.app import wait_for_pending_webhooks
from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, SimulatorRig, make_rig

COVER = "http://cdn.test/covers/conv-1.png"

_seq = itertools.count(1)
_pq_ids = itertools.count(1)


def _assign_seq(mapper: Any, connection: Any, target: Job) -> None:
    if target.seq is None:
        target.seq = next(_seq)


def _assign_pq_id(mapper: Any, connection: Any, target: PlaybackQueue) -> None:
    if target.id is None:
        target.id = next(_pq_ids)


@pytest.fixture(autouse=True)
def _sqlite_shims() -> Iterator[None]:
    """SQLite can't server-generate `Job.seq` / `PlaybackQueue.id` (PG Identity)."""
    event.listen(Job, "before_insert", _assign_seq)
    event.listen(PlaybackQueue, "before_insert", _assign_pq_id)
    yield
    event.remove(Job, "before_insert", _assign_seq)
    event.remove(PlaybackQueue, "before_insert", _assign_pq_id)


@pytest.fixture()
async def sessionmaker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


# ── webhook -> Job ───────────────────────────────────────────────────────────────


class _NotifySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, channel: str, payload: str) -> None:
        self.calls.append((channel, payload))


def _webhook_client(sm: async_sessionmaker[AsyncSession]) -> TestClient:
    from songforge.web.app import create_app
    from songforge.web.routes.webhook import (
        get_notify_dependency,
        get_semaphore_dependency,
        get_session,
    )

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sm() as session:
            yield session

    class _Sem:
        async def release(self, *a: object, **k: object) -> None:
            return None

    app = create_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_notify_dependency] = lambda: _NotifySpy()
    app.dependency_overrides[get_semaphore_dependency] = lambda: _Sem()
    return TestClient(app)


async def _insert_waiting_job(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as session:
        session.add(
            Job(
                job_id="job-1",
                user_id="user-1",
                prompt="p",
                lyrics=None,
                state=JOB_STATE_WAITING_FOR_WEBHOOK,
                task_id="task-1",
                conversion_id_1="conv-1",
                conversion_id_2="conv-2",
                eta=60,
                credit_estimate=1.0,
                webhook_url="http://web:8000/api/generation/webhook",
            )
        )
        await session.commit()


def _body(**overrides: object) -> dict[str, object]:
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


async def test_webhook_persists_album_cover_path_onto_job(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_waiting_job(sessionmaker)
    client = _webhook_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_body(album_cover_path=COVER))

    assert resp.status_code == 200
    async with sessionmaker() as session:
        job = (await session.scalars(select(Job).where(Job.job_id == "job-1"))).one()
    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.album_cover_path == COVER  # type: ignore[attr-defined]


async def test_webhook_without_album_cover_path_leaves_job_cover_null(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_waiting_job(sessionmaker)
    client = _webhook_client(sessionmaker)

    resp = client.post("/api/generation/webhook", json=_body())

    assert resp.status_code == 200
    async with sessionmaker() as session:
        job = (await session.scalars(select(Job).where(Job.job_id == "job-1"))).one()
    assert job.state == JOB_STATE_INGEST_PENDING
    assert job.album_cover_path is None  # type: ignore[attr-defined]


async def test_failure_webhook_with_cover_but_no_conversion_path_creates_no_song(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_waiting_job(sessionmaker)
    client = _webhook_client(sessionmaker)

    resp = client.post(
        "/api/generation/webhook",
        json=_body(conversion_path=None, album_cover_path=COVER),
    )

    assert resp.status_code == 200
    async with sessionmaker() as session:
        job = (await session.scalars(select(Job).where(Job.job_id == "job-1"))).one()
        songs = (await session.scalars(select(Song))).all()
    assert job.state == JOB_STATE_FAILED
    assert job.album_cover_path is None
    assert songs == []


# ── ingest: Job -> Song ──────────────────────────────────────────────────────────


class _Storage:
    def exists(self, key: str) -> bool:
        return False

    def put(self, key: str, data: bytes, content_type: str = "audio/mpeg") -> str:
        return f"https://cdn.test/{key}"


class _Downloader:
    async def download(self, url: str) -> bytes:
        return b"mp3"


class _GenClient:
    async def create(self, **kwargs: object) -> None:
        raise AssertionError("ingest must not create")

    async def get_audio_url_by_id(self, task_id: str) -> str:
        raise AssertionError("by-id not expected on happy path")


async def _ingest(sm: async_sessionmaker[AsyncSession], **job_overrides: object) -> Song:
    from songforge.jobs.ingest import ingest_claimed_job

    fields: dict[str, object] = dict(
        job_id="job-1",
        seq=1,
        user_id="user-1",
        prompt="p",
        lyrics=None,
        state=JOB_STATE_INGEST_PENDING,
        attempts=0,
        available_at=datetime.now(timezone.utc),
        webhook_url="http://web:8000/api/generation/webhook",
        task_id="task-1",
        conversion_id_1="conv-1",
        conversion_id_2="conv-2",
        eta=60,
        credit_estimate=1.0,
        audio_url="http://musicgpt.test/audio/hint-token",
        audio_duration=181.0,
        title="A Generated Song",
        ingest_attempts=0,
        song_id=None,
    )
    fields.update(job_overrides)
    job = Job(**fields)  # type: ignore[arg-type]
    async with sm() as session:
        await ingest_claimed_job(
            job,
            session=session,
            storage=_Storage(),  # type: ignore[arg-type]
            generation_client=_GenClient(),  # type: ignore[arg-type]
            downloader=_Downloader(),  # type: ignore[arg-type]
            settings=Settings(),
        )
        assert job.state == JOB_STATE_READY
        song = await session.get(Song, "conv-1")
        assert song is not None
        return song


async def test_ingest_copies_job_album_cover_path_onto_song(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    song = await _ingest(sessionmaker, album_cover_path=COVER)

    assert song.album_cover_path == COVER  # type: ignore[attr-defined]


async def test_ingest_without_cover_creates_song_with_null_cover(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    song = await _ingest(sessionmaker)

    assert song.album_cover_path is None  # type: ignore[attr-defined]


# ── PointerRecord round-trip ─────────────────────────────────────────────────────


def _view(**extra: object) -> Any:
    from songforge.radio.state import NowPlayingView

    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    return NowPlayingView(
        song_id="song-1",
        title="Song One",
        source="generated",
        object_key="audio/song-1.mp3",
        audio_url="http://cdn.test/audio/song-1.mp3",
        started_at=started,
        ends_at=started + timedelta(seconds=180),
        duration_seconds=180,
        playback_id="pb-1",
        version=3,
        **extra,  # type: ignore[arg-type]
    )


def test_pointer_record_round_trip_preserves_album_cover_path() -> None:
    from songforge.radio.pointer_cache import PointerRecord

    record = PointerRecord.from_view(_view(album_cover_path=COVER))

    restored = PointerRecord.from_json(record.to_json())

    assert restored.album_cover_path == COVER
    assert restored == record


def test_pointer_record_to_response_includes_album_cover_path() -> None:
    from songforge.radio.pointer_cache import PointerRecord

    now = datetime.now(timezone.utc)
    body = PointerRecord.from_view(_view(album_cover_path=COVER)).to_response(server_time=now)

    assert body["album_cover_path"] == COVER


def test_pointer_record_to_response_surfaces_null_cover() -> None:
    from songforge.radio.pointer_cache import PointerRecord

    now = datetime.now(timezone.utc)
    body = PointerRecord.from_view(_view(album_cover_path=None)).to_response(server_time=now)

    assert "album_cover_path" in body
    assert body["album_cover_path"] is None


def test_pointer_record_from_json_defaults_missing_cover_to_none() -> None:
    """Forward-compat: a value cached in Redis before this deploy lacks the key."""
    from songforge.radio.pointer_cache import PointerRecord

    payload = json.loads(PointerRecord.from_view(_view(album_cover_path=COVER)).to_json())
    payload.pop("album_cover_path")

    restored = PointerRecord.from_json(json.dumps(payload))

    assert restored.album_cover_path is None


def test_now_playing_view_to_response_includes_album_cover_path() -> None:
    now = datetime.now(timezone.utc)

    body = _view(album_cover_path=COVER).to_response(server_time=now)

    assert body["album_cover_path"] == COVER


# ── simulator: deterministic placeholder per conversion id ───────────────────────


@pytest.fixture()
async def rig() -> AsyncIterator[SimulatorRig]:
    r = make_rig()
    async with r.client:
        yield r


async def test_simulator_webhook_carries_non_null_album_cover_path(rig: SimulatorRig) -> None:
    await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)
    await wait_for_pending_webhooks(rig.app)

    payload = rig.receiver.received[0]
    assert payload["album_cover_path"]
    assert isinstance(payload["album_cover_path"], str)


async def test_simulator_cover_is_deterministic_per_conversion_id(rig: SimulatorRig) -> None:
    """Same conversion id -> same cover across the webhook and `/byId` (and repeat calls);
    different conversions -> different covers."""
    create = (await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)).json()
    await wait_for_pending_webhooks(rig.app)

    webhook_cover = rig.receiver.received[0]["album_cover_path"]
    by_id_1 = (await rig.client.get("/byId", params={"task_id": create["task_id"]})).json()
    by_id_2 = (await rig.client.get("/byId", params={"task_id": create["task_id"]})).json()

    assert by_id_1["album_cover_path"]
    assert by_id_1["album_cover_path"] == by_id_2["album_cover_path"]
    assert by_id_1["album_cover_path"] == webhook_cover

    create_b = (await rig.client.post(CREATE_PATH, json=DEFAULT_BODY)).json()
    await wait_for_pending_webhooks(rig.app)
    by_id_b = (await rig.client.get("/byId", params={"task_id": create_b["task_id"]})).json()
    assert by_id_b["album_cover_path"] != by_id_1["album_cover_path"]


# ── migration 0008 ───────────────────────────────────────────────────────────────

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0008_album_cover_path.py"
)


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("migration_0008", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0008_chains_after_0007() -> None:
    mod = _load_migration()

    assert mod.revision == "0008_album_cover_path"
    assert mod.down_revision == "0007_correlation_id"


def test_migration_0008_adds_nullable_column_without_touching_existing_rows() -> None:
    """Runs the real upgrade()/downgrade() through alembic Operations on in-memory SQLite
    against legacy-shaped `songs` and `jobs` tables (0001-0007 use PG-specific DDL, so they
    are not replayed; only the table shapes 0008 acts on are recreated). Migration 0008 adds
    the column to BOTH tables, so both must exist in the legacy fixture (a real pre-0008 DB
    has a `jobs` table from migration 0003)."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = _load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE songs (id VARCHAR(64) PRIMARY KEY, title VARCHAR(255) NOT NULL,"
                " source VARCHAR(16) NOT NULL, object_key VARCHAR(512) NOT NULL,"
                " duration_seconds INTEGER, created_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO songs (id, title, source, object_key, duration_seconds)"
                " VALUES ('old-1', 'Old Song', 'static', 'audio/old-1.mp3', 120)"
            )
        )
        # Minimal legacy `jobs` table: 0008 adds a column to it too, so it must pre-exist.
        conn.execute(
            sa.text("CREATE TABLE jobs (job_id VARCHAR(64) PRIMARY KEY, title VARCHAR(255))")
        )
        conn.execute(
            sa.text("INSERT INTO jobs (job_id, title) VALUES ('job-old-1', 'Old Job')")
        )
        with Operations.context(MigrationContext.configure(conn)):
            mod.upgrade()

        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("songs")}
        assert "album_cover_path" in cols
        assert cols["album_cover_path"]["nullable"] is True
        row = conn.execute(
            sa.text(
                "SELECT id, title, source, object_key, duration_seconds, album_cover_path"
                " FROM songs"
            )
        ).one()
        assert tuple(row) == ("old-1", "Old Song", "static", "audio/old-1.mp3", 120, None)

        # The jobs column is added nullable too, leaving the existing job row untouched.
        job_cols = {c["name"]: c for c in sa.inspect(conn).get_columns("jobs")}
        assert "album_cover_path" in job_cols
        assert job_cols["album_cover_path"]["nullable"] is True
        job_row = conn.execute(
            sa.text("SELECT job_id, title, album_cover_path FROM jobs")
        ).one()
        assert tuple(job_row) == ("job-old-1", "Old Job", None)

        with Operations.context(MigrationContext.configure(conn)):
            mod.downgrade()
        cols_after = {c["name"] for c in sa.inspect(conn).get_columns("songs")}
        assert "album_cover_path" not in cols_after
        job_cols_after = {c["name"] for c in sa.inspect(conn).get_columns("jobs")}
        assert "album_cover_path" not in job_cols_after


def test_song_model_column_is_nullable() -> None:
    column = Song.__table__.c.get("album_cover_path")

    assert column is not None
    assert column.nullable is True
