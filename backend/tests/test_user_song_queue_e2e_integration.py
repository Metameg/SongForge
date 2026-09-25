"""End-to-end proof of issue #14's four acceptance criteria against the REAL simulator
(issue #11), a real static-library `Song`, and real Postgres -- sibling to
`tests/test_webhook_ingest_e2e_integration.py` (issue #13's webhook->ingest->READY e2e),
extended past READY through enqueue -> advance -> the pointer + a published Redis event.

Flow: drive the real simulator's create -> real webhook delivery onto OUR real
`/api/generation/webhook` route -> real `claim_next_ingest_job` + `ingest_claimed_job`
against the real simulator's `/byId`/`/audio/{token}` and a moto S3 bucket (issue #13's
proven path) -- THEN (issue #14's new ground): assert the READY song was enqueued onto
`playback_queue`, seed a real static `Song` + initialize the radio pointer on it, and
call the real `radio.coordinator.advance` to prove the user song lands on the pointer
and is published to Redis pub/sub.

Skips (not fails) if Postgres or Redis is unreachable, or moto isn't installed -- same
convention as `test_webhook_ingest_e2e_integration.py`.

RED phase (phase 1): `advance()` doesn't yet consult `playback_queue` and
`jobs.ingest` doesn't yet enqueue -- this test fails on the `playback_queue` row
assertion and/or the final pointer assertion, not on collection (it also exercises the
still-`NotImplementedError`-stubbed `radio.queue.pop_next_user_song` indirectly via
`advance`, once phase 3 wires that call in -- today it simply never reaches the queue
at all, so this fails on "no row was enqueued" first).
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

asyncpg = pytest.importorskip("asyncpg")

_HAS_MOTO = importlib.util.find_spec("moto") is not None

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _HAS_MOTO, reason="moto not installed"),
]

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_SETTINGS_URL = "postgresql+asyncpg://songforge:songforge@127.0.0.1:55432/songforge"
_SETTINGS_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", _DEFAULT_SETTINGS_URL)
_ASYNCPG_DSN = _SETTINGS_DATABASE_URL.replace("+asyncpg", "")
_CONNECT_TIMEOUT_SECONDS = 1.5
_TEST_JOB_PREFIX = "test-issue14-e2e-"
_TEST_STATIC_SONG_ID = "test-issue14-static-song"


def _postgres_reachable() -> bool:
    parts = urlsplit(_ASYNCPG_DSN)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_postgres() -> None:
    if not _postgres_reachable():
        pytest.skip(f"Postgres unreachable at {_ASYNCPG_DSN!r}")


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_require_postgres: None) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = _SETTINGS_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND_DIR), env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the integration Postgres:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def _settings() -> Any:
    from songforge.config import Settings

    return Settings(
        _env={
            "DATABASE_URL": _SETTINGS_DATABASE_URL,
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "S3_ENDPOINT_URL": "https://s3.us-east-1.amazonaws.com",
            "S3_ACCESS_KEY_ID": "test",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_BUCKET": "songforge-e2e-test",
            "S3_REGION": "us-east-1",
        }
    )


@pytest.fixture()
async def _clean_rows() -> Any:
    conn = await asyncpg.connect(_ASYNCPG_DSN, timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        # `playback_queue` rows must go first (`fk_playback_queue_job_id_jobs`) -- both
        # on setup (a prior run's own cleanup could itself have been interrupted, e.g.
        # by a crashed test process) and on teardown, before the `jobs` delete below.
        await conn.execute(
            "DELETE FROM playback_queue WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%"
        )
        await conn.execute("DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%")
        await conn.execute("DELETE FROM radio_state WHERE id = 1")
        yield
        await conn.execute("DELETE FROM radio_state WHERE id = 1")
        await conn.execute(
            "DELETE FROM playback_queue WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%"
        )
        await conn.execute("DELETE FROM jobs WHERE job_id LIKE $1", f"{_TEST_JOB_PREFIX}%")
        await conn.execute(
            "DELETE FROM songs WHERE id = $1", _TEST_STATIC_SONG_ID
        )
        # The generated song is keyed by the simulator's real (random) conversion_id_1
        # -- the test's own `finally` block deletes it by id once known.
    finally:
        await conn.close()


async def _insert_waiting_job(
    sessionmaker: Any,
    job_id: str,
    *,
    task_id: str,
    conversion_id_1: str,
    conversion_id_2: str,
) -> None:
    from songforge.models import JOB_STATE_WAITING_FOR_WEBHOOK, Job

    async with sessionmaker() as session:
        session.add(
            Job(
                job_id=job_id, user_id="u1", prompt="a test prompt",
                state=JOB_STATE_WAITING_FOR_WEBHOOK, task_id=task_id,
                conversion_id_1=conversion_id_1, conversion_id_2=conversion_id_2,
            )
        )
        await session.commit()


class _RecordingRedis:
    """Real-enough async Redis double so `advance`'s best-effort set/publish is
    directly observable without needing a live Redis for this integration test
    (Postgres is the piece under real integration test here; Redis's own set/publish
    contract already has dedicated coverage in `test_radio_coordinator.py`)."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str]] = []
        self.publish_calls: list[tuple[str, str]] = []

    async def set(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))

    async def publish(self, channel: str, message: str) -> None:
        self.publish_calls.append((channel, message))


async def test_submit_ingest_enqueue_advance_song_lands_on_the_pointer_and_publishes(
    _clean_rows: None,
) -> None:
    """Criteria #1-#4 end to end: a READY user song is enqueued (#1), `advance()` pops
    it ahead of the static library (#2), and the resulting pointer is published (#4's
    "heard live" proxy at the pointer/pub-sub layer -- the audio element itself is a
    frontend concern, out of this backend e2e's scope)."""
    from moto import mock_aws
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from songforge.jobs.generation_client import HttpGenerationClient
    from songforge.jobs.ingest import (
        HttpAudioDownloader,
        claim_next_ingest_job,
        ingest_claimed_job,
    )
    from songforge.models import (
        JOB_STATE_READY,
        Job,
        PlaybackQueue,
        RadioState,
        Song,
        SOURCE_GENERATED,
        SOURCE_STATIC,
    )
    from songforge.radio.coordinator import advance, initialize_if_absent
    from songforge.radio.history import RedisRecentHistoryStore
    from songforge.simulator.app import create_app as create_sim_app
    from songforge.simulator.app import wait_for_pending_webhooks
    from songforge.storage import ObjectStorage, audio_key
    from songforge.web.app import create_app as create_web_app
    from songforge.web.routes import webhook as webhook_module
    from tests.simulator_helpers import CREATE_PATH, DEFAULT_BODY, make_sim_client

    settings = _settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    # (Deliberately no `history`/redis-writes need a REAL Redis: `advance` only reads
    # `RecentHistoryStore.recent_ids`/`record`, which a trivial in-memory stub already
    # covers in the unit suite -- this file's job is to prove real Postgres wiring.)
    class _StubHistory:
        async def recent_ids(self) -> set[str]:
            return set()

        async def record(self, song_id: str) -> None:
            return None

    async def _get_session() -> Any:
        async with sessionmaker() as session:
            yield session

    web_app = create_web_app(settings)
    web_app.dependency_overrides[webhook_module.get_session] = _get_session
    webhook_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_app), base_url="http://web.test"
    )

    sim_app = create_sim_app(http_client=webhook_client)
    sim_client = make_sim_client(sim_app)

    with mock_aws():
        storage = ObjectStorage.from_settings(settings)
        storage.ensure_bucket()

        handles: dict[str, Any] = {}
        try:
            # 1) Seed the static library BEFORE anything else, so the coordinator's
            # initial pointer is deterministically the static song (mirrors
            # `test_radio_queue_priority.py`'s ordering note).
            async with sessionmaker() as session:
                session.add(
                    Song(
                        id=_TEST_STATIC_SONG_ID,
                        title="E2E Static Filler",
                        source=SOURCE_STATIC,
                        object_key=f"audio/{_TEST_STATIC_SONG_ID}.mp3",
                        duration_seconds=180,
                    )
                )
                await session.commit()

            # Note: the shared integration Postgres may already carry other seeded
            # static songs -- `initialize_if_absent`'s `pick_static` may land on any
            # of them (or on ours), not necessarily `_TEST_STATIC_SONG_ID` specifically.
            # The only thing this test needs is "some static song is playing", so the
            # later advance-to-generated assertion is a genuine before/after contrast.
            async with sessionmaker() as session:
                created = await initialize_if_absent(session, _StubHistory())
                assert created is True
                pointer = await session.get(RadioState, 1)
                assert pointer is not None
                assert pointer.source == SOURCE_STATIC
                current_version = pointer.version

            # 2) Submit -> real webhook delivery -> INGEST_PENDING (issue #13's proven
            # path, replayed here as this test's setup).
            async with sim_client:
                create_resp = await sim_client.post(
                    CREATE_PATH,
                    json={
                        **DEFAULT_BODY,
                        "webhook_url": "http://web.test/api/generation/webhook",
                    },
                )
                handles = create_resp.json()
                job_id = f"{_TEST_JOB_PREFIX}happy"
                await _insert_waiting_job(
                    sessionmaker, job_id, task_id=handles["task_id"],
                    conversion_id_1=handles["conversion_id_1"],
                    conversion_id_2=handles["conversion_id_2"],
                )
                await wait_for_pending_webhooks(sim_app)

            # 3) Real ingest: download + upload to R2 + READY (issue #13's proven
            # path) -- and, per issue #14 criterion #1, enqueue onto playback_queue.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sim_app), base_url="http://simulator:8080"
            ) as sim_io_client:
                downloader = HttpAudioDownloader(
                    sim_io_client,
                    timeout=settings.ingest_download_timeout_seconds,
                    max_bytes=settings.ingest_max_download_bytes,
                )
                generation_client = HttpGenerationClient(settings, sim_io_client)

                async with sessionmaker() as session:
                    claimed = await claim_next_ingest_job(session)
                    assert claimed is not None
                    assert claimed.job_id == job_id
                    await ingest_claimed_job(
                        claimed, session=session, storage=storage,
                        downloader=downloader, generation_client=generation_client,
                        settings=settings,
                    )
                    await session.commit()

            async with sessionmaker() as session:
                final_job = await session.get(Job, job_id)
                assert final_job is not None
                assert final_job.state == JOB_STATE_READY
                song_id = final_job.song_id
                assert song_id is not None

            # Criterion #1: the READY song is enqueued onto the authoritative queue.
            async with sessionmaker() as session:
                rows = (
                    await session.scalars(
                        select(PlaybackQueue).where(PlaybackQueue.song_id == song_id)
                    )
                ).all()
                assert len(rows) == 1
                assert rows[0].played_at is None

            # 4) Criteria #2/#4: `advance()` pops the user song ahead of the static
            # library, and best-effort publishes the resolved pointer.
            redis = _RecordingRedis()
            async with sessionmaker() as session:
                applied = await advance(
                    session,
                    _StubHistory(),
                    expected_version=current_version,
                    redis=redis,  # type: ignore[arg-type]
                )
                assert applied is True

            async with sessionmaker() as session:
                after = await session.get(RadioState, 1)
                assert after is not None
                assert after.song_id == song_id
                assert after.source == SOURCE_GENERATED

                queue_row = (
                    await session.scalars(
                        select(PlaybackQueue).where(PlaybackQueue.song_id == song_id)
                    )
                ).one()
                assert queue_row.played_at is not None  # consumed by the applied CAS

            assert len(redis.publish_calls) == 1
            _channel, payload_raw = redis.publish_calls[0]
            payload = json.loads(payload_raw)
            assert payload["song_id"] == song_id
        finally:
            if handles:
                async with sessionmaker() as session:
                    # The pointer now references the generated song (`advance` moved
                    # it there) -- clear it FIRST, or the Song delete below trips
                    # `radio_state_song_id_fkey`. `_clean_rows`' own post-yield
                    # teardown re-deletes `radio_state` too; a second delete of an
                    # already-gone row is just a 0-row no-op.
                    await session.execute(delete(RadioState).where(RadioState.id == 1))
                    await session.commit()
                    # Delete playback_queue rows BEFORE the Job they reference
                    # (fk_playback_queue_job_id_jobs) -- and before the Song too
                    # (fk_playback_queue_song_id_songs), or Postgres rejects the
                    # Job/Song delete below with a foreign-key violation.
                    conv_id = handles.get("conversion_id_1")
                    if conv_id is not None:
                        queue_rows = (
                            await session.scalars(
                                select(PlaybackQueue).where(PlaybackQueue.song_id == conv_id)
                            )
                        ).all()
                        for row in queue_rows:
                            await session.delete(row)
                        await session.commit()
                    job_row = await session.get(Job, job_id)
                    if job_row is not None:
                        await session.delete(job_row)
                        await session.commit()
                    if conv_id is not None:
                        song = await session.get(Song, conv_id)
                        if song is not None:
                            await session.delete(song)
                            await session.commit()
            await engine.dispose()
            await webhook_client.aclose()
