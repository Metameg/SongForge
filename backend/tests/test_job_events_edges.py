"""Edge/error-path coverage for the per-user `job-progress` / `job-ready` events
(issue #37): the `/events` wire formatter's fallback + whitelist, and the worker drain
hooks' negative paths. Happy paths live in `test_worker_dispatch.py`,
`test_worker_ingest.py`, `test_webhook_route.py` and `test_events_sse.py`."""

from __future__ import annotations

import json
from typing import Any

import pytest

import songforge.worker.dispatch as worker_dispatch_module
import songforge.worker.ingest as worker_ingest_module
from songforge.config import Settings
from songforge.jobs.ingest import DEFAULT_SONG_TITLE
from songforge.web.routes.events import _format_user_event
from songforge.worker.dispatch import _drain_ready_jobs
from songforge.worker.ingest import _drain_ingest_pending


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


class _NullSession:
    async def commit(self) -> None:
        return None


class _NullSessionCtx:
    async def __aenter__(self) -> _NullSession:
        return _NullSession()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _sessionmaker():  # type: ignore[no-untyped-def]
    return _NullSessionCtx()


def _parse_frame(frame: str | None) -> tuple[str, dict[str, Any]]:
    assert frame is not None
    event_line, data_line = frame.strip().split("\n")
    assert event_line.startswith("event: ") and data_line.startswith("data: ")
    return event_line[len("event: "):], json.loads(data_line[len("data: "):])


# ── /events wire formatter ────────────────────────────────────────────────────────


def test_unknown_event_value_is_dropped() -> None:
    assert _format_user_event({"event": "job-exploded", "job_id": "j1"}) is None


def test_absent_event_key_falls_back_to_job_failed_without_leaking_fields() -> None:
    name, payload = _parse_frame(
        _format_user_event({"job_id": "j1", "user_id": "secret-user", "state": "X", "title": "T"})
    )
    assert name == "job-failed"
    assert payload == {"job_id": "j1"}


@pytest.mark.parametrize(
    "message",
    [
        {"event": "job-failed", "job_id": "j1", "user_id": "secret-user", "eta": 5},
        {"event": "job-progress", "job_id": "j1", "state": "S", "eta": 1, "user_id": "secret-user"},
        {"event": "job-ready", "job_id": "j1", "song_id": "s", "title": "T", "user_id": "secret-user"},
    ],
)
def test_known_events_never_leak_the_user_id(message: dict[str, Any]) -> None:
    frame = _format_user_event(message)
    assert frame is not None
    assert "secret-user" not in frame


def test_empty_event_value_is_dropped() -> None:
    assert _format_user_event({"event": "", "job_id": "j1"}) is None


def test_job_progress_message_missing_eta_serialises_eta_as_null() -> None:
    _, payload = _parse_frame(
        _format_user_event({"event": "job-progress", "job_id": "j1", "state": "S"})
    )
    assert payload == {"job_id": "j1", "state": "S", "eta": None}


def test_job_ready_message_missing_fields_serialises_them_as_null() -> None:
    _, payload = _parse_frame(_format_user_event({"event": "job-ready", "job_id": "j1"}))
    assert payload == {"job_id": "j1", "song_id": None, "title": None}


# ── dispatch drain hook ───────────────────────────────────────────────────────────


class _DispatchJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.user_id = "user-1"
        self.state = "QUEUED"
        self.eta: int | None = None


async def _noop_notify(job_id: str) -> None:
    return None


async def test_dispatch_progress_carries_a_null_eta_when_the_api_gave_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remaining = iter(["job-1", None])

    async def _claim(session: object) -> _DispatchJob | None:
        job_id = next(remaining)
        return None if job_id is None else _DispatchJob(job_id)

    async def _dispatch(job: _DispatchJob, **kwargs: object) -> bool:
        job.state = "WAITING_FOR_WEBHOOK"
        return True

    monkeypatch.setattr(worker_dispatch_module, "claim_next_job", _claim)
    monkeypatch.setattr(worker_dispatch_module, "dispatch_claimed_job", _dispatch)
    published: list[tuple[str, str]] = []

    async def _publish(user_id: str, message: str) -> None:
        published.append((user_id, message))

    await _drain_ready_jobs(
        _sessionmaker, object(), object(), _settings(), _noop_notify,  # type: ignore[arg-type]
        publish_user_event=_publish,
    )

    assert json.loads(published[0][1])["eta"] is None


async def test_dispatch_without_a_publisher_never_reads_user_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Bare:
        job_id = "job-1"  # no user_id/state/eta: legacy callers' minimal stand-in

    remaining = iter([_Bare(), None])

    async def _claim(session: object) -> object | None:
        return next(remaining)

    async def _dispatch(job: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(worker_dispatch_module, "claim_next_job", _claim)
    monkeypatch.setattr(worker_dispatch_module, "dispatch_claimed_job", _dispatch)

    await _drain_ready_jobs(
        _sessionmaker, object(), object(), _settings(), _noop_notify  # type: ignore[arg-type]
    )


# ── ingest drain hook ─────────────────────────────────────────────────────────────


class _IngestJob:
    def __init__(self, job_id: str, title: str | None = "Song") -> None:
        self.job_id = job_id
        self.user_id = "user-1"
        self.state = "INGEST_PENDING"
        self.song_id: str | None = None
        self.title = title


async def _drain_one_ingest(
    monkeypatch: pytest.MonkeyPatch,
    job: _IngestJob,
    final: Any,
    **hooks: Any,
) -> None:
    remaining = iter([job, None])

    async def _claim(session: object) -> _IngestJob | None:
        return next(remaining)

    async def _ingest(j: _IngestJob, **kwargs: object) -> None:
        final(j)

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _ingest)
    await _drain_ingest_pending(
        _sessionmaker,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _settings(),
        **hooks,
    )


def _ready(song_id: str | None) -> Any:
    def _apply(j: _IngestJob) -> None:
        j.state = "READY"
        j.song_id = song_id

    return _apply


async def test_ingest_ready_without_a_song_id_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []

    async def _publish(user_id: str, message: str) -> None:
        published.append((user_id, message))

    await _drain_one_ingest(monkeypatch, _IngestJob("job-1"), _ready(None), publish_user_event=_publish)

    assert published == []


async def test_ingest_ready_with_no_title_publishes_untitled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, str]] = []

    async def _publish(user_id: str, message: str) -> None:
        published.append((user_id, message))

    await _drain_one_ingest(
        monkeypatch, _IngestJob("job-1", title=None), _ready("song-1"), publish_user_event=_publish
    )

    assert json.loads(published[0][1])["title"] == DEFAULT_SONG_TITLE == "Untitled"


async def test_ingest_raising_publisher_does_not_skip_notify_ready_or_semaphore_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[str] = []
    released: list[str] = []

    async def _boom(user_id: str, message: str) -> None:
        raise RuntimeError("redis down")

    async def _notify_ready(song_id: str) -> None:
        notified.append(song_id)

    class _Sem:
        async def release(self, user_id: str) -> None:
            released.append(user_id)

    await _drain_one_ingest(
        monkeypatch,
        _IngestJob("job-1"),
        _ready("song-1"),
        notify_ready=_notify_ready,
        semaphore=_Sem(),
        publish_user_event=_boom,
    )

    assert notified == ["song-1"]
    assert released == ["user-1"]


async def test_ingest_without_a_publisher_never_reads_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Bare:
        job_id = "job-1"
        state = "READY"
        song_id = "song-1"
        # no user_id/title: legacy minimal stand-in

    remaining = iter([_Bare(), None])

    async def _claim(session: object) -> object | None:
        return next(remaining)

    async def _ingest(job: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker_ingest_module, "claim_next_ingest_job", _claim)
    monkeypatch.setattr(worker_ingest_module, "ingest_claimed_job", _ingest)

    await _drain_ingest_pending(
        _sessionmaker,
        object(), object(), object(),  # type: ignore[arg-type]
        _settings(),
    )
