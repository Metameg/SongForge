"""Edge tests for `GET /events` — SSE push + Redis pub/sub fan-out (issue #10).

Drives the FastAPI app through real ASGI (`httpx.AsyncClient` + `httpx.ASGITransport`,
not the synchronous `TestClient`) so the test, the fake Redis pub/sub double, and the
app's own background broadcaster task all cooperatively share ONE asyncio event loop —
required to interleave "read the on-connect frame" / "publish a change" / "read the
relayed frame" within a single test coroutine (pytest-asyncio `asyncio_mode = auto`,
per `pyproject.toml`). The app's startup/shutdown (issue #10 wires the per-instance
pub/sub relay via a FastAPI `lifespan`) is driven explicitly via
`app.router.lifespan_context(app)` rather than `TestClient`'s context-manager form, for
the same reason: full control over when the broadcaster's listener task is running
relative to the test's own awaits.

Acceptance criteria covered here (see `.orchestrator/CONTEXT.md`):
  1. Listeners receive a song-change event over SSE the moment the station advances
     (`test_events_relays_a_published_pointer_to_a_connected_client`).
  2. Every app instance relays a pub/sub-published pointer to its own SSE listeners,
     via ONE Redis subscription fanned out in-process — not one subscription (or one
     extra Redis read) per client
     (`test_events_fans_out_one_subscription_to_multiple_clients_without_extra_redis_reads`).
  3. A received pub/sub pointer updates `app.state.pointer_cache` so `/now-playing`
     reflects it immediately, not after the cache TTL
     (`test_pubsub_relay_updates_pointer_cache_so_now_playing_reflects_it_immediately`).
  4. On connect, the current pointer is sent immediately (sync-on-arrival), and a ~30s
     heartbeat (injectable/short here) re-emits it so a dropped pub/sub message is
     still caught by a client that never disconnected.

NOTE: `songforge.web.routes.events` and `create_app`'s `redis=` startup-wiring
parameter do not exist yet (issue #10, RED phase). Importing them at module scope
would fail collection of this entire file. They are therefore imported/used only
inside each test function (mirroring the NOTE block in `test_now_playing.py`), so
these new tests fail explicitly (ImportError / TypeError) without breaking collection
of the rest of the suite.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from songforge.models import Base
from songforge.radio.pointer_cache import PointerRecord
from songforge.radio.state import get_now_playing
from songforge.web.app import create_app

# The context pack's stated default pub/sub channel (issue #10). Not read from
# `config.py` here since that setting doesn't exist yet either (Phase 2/3 wiring) —
# matches this repo's convention of hardcoding the stated default in RED-phase tests
# (see `RADIO_POINTER_REDIS_KEY` in `test_now_playing.py`).
CHANNEL = "radio:pointer:changed"

EXPECTED_BODY_KEYS = {
    "status",
    "song_id",
    "title",
    "source",
    "object_key",
    "audio_url",
    "started_at",
    "ends_at",
    "duration",
    "playback_id",
    "version",
    "server_time",
}


def _record(*, song_id: str = "song-1", version: int = 1, playback_id: str = "pb-1") -> PointerRecord:
    now = datetime.now(timezone.utc)
    return PointerRecord(
        song_id=song_id,
        title=f"Title for {song_id}",
        source="static",
        object_key=f"audio/{song_id}.mp3",
        audio_url=f"http://cdn.test/audio/{song_id}.mp3",
        started_at=(now - timedelta(seconds=5)).isoformat(),
        ends_at=(now + timedelta(seconds=175)).isoformat(),
        duration_seconds=180,
        playback_id=playback_id,
        version=version,
    )


class _FakePubSub:
    """Minimal async pubsub double mirroring `redis.asyncio.client.PubSub`'s
    subscribe/psubscribe/get_message/close surface — the surface both broadcasters'
    listener loops need. One instance is created per `redis.pubsub()` call, matching
    real Redis. `psubscribe`/pattern `get_message` (issue #16: `UserEventBroadcaster`
    PSUBSCRIBEs a wildcard pattern rather than a fixed channel) mirror real Redis's
    `pmessage` shape: `{"type": "pmessage", "pattern": ..., "channel": ..., "data": ...}`."""

    def __init__(self, redis: "_FakeRedis") -> None:
        self._redis = redis
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._channels: set[str] = set()
        self._patterns: set[str] = set()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._redis.subscribe_calls += 1
        self._redis._subscribers.append(self)

    async def psubscribe(self, pattern: str) -> None:
        self._patterns.add(pattern)
        self._redis._subscribers.append(self)

    async def get_message(
        self,
        ignore_subscribe_messages: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        self.closed = True
        if self in self._redis._subscribers:
            self._redis._subscribers.remove(self)


class _FakeRedis:
    """Fake async Redis double covering the surface `/events` needs: `publish` +
    `pubsub()` (issue #10's fan-out; issue #16's per-user PSUBSCRIBE fan-out), plus
    `get`/`set` in case a route falls back through the existing `/now-playing`
    pointer-cache chain. File-local, per this repo's per-file stub convention (see
    `_StubHistory` in `test_radio_coordinator.py`, `_FakeRedis` in `test_now_playing.py`)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._subscribers: list[_FakePubSub] = []
        self.publish_calls: list[tuple[str, str]] = []
        self.subscribe_calls = 0

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def publish(self, channel: str, message: str) -> None:
        self.publish_calls.append((channel, message))
        for sub in list(self._subscribers):
            if channel in sub._channels:
                await sub._queue.put({"type": "message", "channel": channel, "data": message})
            for pattern in sub._patterns:
                if fnmatch.fnmatchcase(channel, pattern):
                    await sub._queue.put(
                        {
                            "type": "pmessage",
                            "pattern": pattern,
                            "channel": channel,
                            "data": message,
                        }
                    )

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self)


async def _sse_events(resp: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Parse an SSE byte stream into `(event, data)` pairs as they arrive.

    A single generator is created per stream and pulled from repeatedly
    (`await gen.__anext__()`) so tests can interleave "read one event" with their own
    actions (e.g. publishing a change) between reads.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
        elif line == "":
            if event_name is not None:
                yield event_name, json.loads("".join(data_lines))
            event_name = None
            data_lines = []


@asynccontextmanager
async def _running_app(
    redis: _FakeRedis, *, heartbeat_seconds: float | None = None
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Build the app wired to `redis`, serve it over a real socket, and hand back a
    client sharing this coroutine's event loop.

    Served by a `uvicorn` instance bound to an ephemeral port (`port=0`) and awaited as
    a task in THIS event loop, so the app's background broadcaster task, the fake Redis
    pub/sub double, and the test coroutine all share one loop — and, critically, an
    *infinite* SSE `StreamingResponse` can be consumed incrementally. A real socket is
    required here: `httpx.ASGITransport` accumulates the entire response body and only
    returns the response once the ASGI app signals `more_body=False`, so it deadlocks
    forever on a never-completing SSE stream. uvicorn runs the FastAPI `lifespan`, which
    starts (and, on `should_exit`, stops) the per-instance `PointerBroadcaster`.

    Contract this fixes for issue #10:
      - `create_app(redis=...)` gains an optional keyword the startup pub/sub relay
        uses in place of the real `get_redis()`, so the app stays testable with no
        live datastore (mirrors `.orchestrator/CONTEXT.md`'s "keep the app importable
        /testable with no live datastore" instruction).
      - `songforge.web.routes.events` exposes `get_heartbeat_seconds`, a `Depends`-
        overridable dependency (same pattern as `now_playing.get_redis_dependency`),
        so tests can shrink the ~30s heartbeat to something fast.
    """
    from songforge.web.routes import events
    from songforge.web.routes.now_playing import (
        get_pg_loader_dependency,
        get_redis_dependency,
        get_session,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app = create_app(redis=cast(Any, redis))
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis_dependency] = lambda: redis
    app.dependency_overrides[get_pg_loader_dependency] = lambda: get_now_playing
    if heartbeat_seconds is not None:
        app.dependency_overrides[events.get_heartbeat_seconds] = lambda: heartbeat_seconds

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        # Wait for bind + ASGI startup (lifespan -> broadcaster.start()) to complete.
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield app, client
    finally:
        server.should_exit = True
        await serve_task


async def test_events_emits_idle_event_when_no_pointer_yet() -> None:
    """Symmetric with `/now-playing`'s 503 idle body (criterion: keep the idle
    contract consistent): no pointer anywhere yet -> an `idle` SSE event, not a
    fabricated playing state, and the stream stays open rather than erroring."""
    redis = _FakeRedis()
    async with _running_app(redis) as (_app, client):
        async with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            name, payload = await asyncio.wait_for(_sse_events(resp).__anext__(), timeout=2.0)
            assert name == "idle"
            assert payload == {"status": "idle"}


async def test_events_emits_current_pointer_as_song_change_on_connect() -> None:
    """Criterion #1/#4: on connect, the listener is immediately sent the current
    pointer (sync-on-arrival), shaped exactly like the `/now-playing` body."""
    redis = _FakeRedis()
    async with _running_app(redis) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=5, playback_id="pb-5"))
        async with client.stream("GET", "/events") as resp:
            assert resp.status_code == 200
            name, payload = await asyncio.wait_for(_sse_events(resp).__anext__(), timeout=2.0)
            assert name == "song-change"
            assert EXPECTED_BODY_KEYS.issubset(payload.keys())
            assert payload["status"] == "playing"
            assert payload["song_id"] == "song-1"
            assert payload["version"] == 5
            assert payload["playback_id"] == "pb-5"


async def test_events_relays_a_published_pointer_to_a_connected_client() -> None:
    """Criterion #1/#2: a pointer published to the pub/sub channel after connect is
    relayed to the already-connected client as a fresh `song-change` event — the
    push, not a poll, is what tells the listener the station advanced."""
    redis = _FakeRedis()
    async with _running_app(redis) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=1, playback_id="pb-1"))
        async with client.stream("GET", "/events") as resp:
            events_iter = _sse_events(resp)
            name, payload = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert name == "song-change"
            assert payload["song_id"] == "song-1"

            next_record = _record(song_id="song-2", version=2, playback_id="pb-2")
            await redis.publish(CHANNEL, next_record.to_json())

            name2, payload2 = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert name2 == "song-change"
            assert payload2["song_id"] == "song-2"
            assert payload2["playback_id"] == "pb-2"
            assert payload2["version"] == 2


async def test_events_fans_out_one_subscription_to_multiple_clients_without_extra_redis_reads() -> (
    None
):
    """Criterion #2 + the datastore-decoupling thesis: N connected SSE clients share
    ONE Redis pub/sub subscription (the per-instance broadcaster), not one per client —
    listener count must not multiply Redis load."""
    redis = _FakeRedis()
    async with _running_app(redis) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=1, playback_id="pb-1"))

        async with (
            client.stream("GET", "/events") as resp1,
            client.stream("GET", "/events") as resp2,
            client.stream("GET", "/events") as resp3,
        ):
            iters = [_sse_events(r) for r in (resp1, resp2, resp3)]
            first_events = [
                await asyncio.wait_for(it.__anext__(), timeout=2.0) for it in iters
            ]
            assert all(name == "song-change" for name, _ in first_events)
            assert all(payload["song_id"] == "song-1" for _, payload in first_events)

            next_record = _record(song_id="song-2", version=2, playback_id="pb-2")
            await redis.publish(CHANNEL, next_record.to_json())

            relayed = [await asyncio.wait_for(it.__anext__(), timeout=2.0) for it in iters]
            assert all(name == "song-change" for name, _ in relayed)
            assert all(payload["song_id"] == "song-2" for _, payload in relayed)

        # Exactly ONE `pubsub().subscribe()` call for the app's whole lifetime,
        # regardless of 3 connected clients -- proves the fan-out is in-process, not
        # one Redis subscription (or read) per client.
        assert redis.subscribe_calls == 1


async def test_events_heartbeat_reemits_current_pointer_periodically() -> None:
    """Criterion #4: a ~30s heartbeat (shrunk here so the test is fast) re-emits the
    current pointer even with no new pub/sub message — covers a dropped pub/sub
    message where nobody disconnected."""
    redis = _FakeRedis()
    async with _running_app(redis, heartbeat_seconds=0.05) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=1, playback_id="pb-1"))
        async with client.stream("GET", "/events") as resp:
            events_iter = _sse_events(resp)
            first_name, first_payload = await asyncio.wait_for(
                events_iter.__anext__(), timeout=2.0
            )
            assert first_name == "song-change"

            second_name, second_payload = await asyncio.wait_for(
                events_iter.__anext__(), timeout=2.0
            )
            assert second_name == "song-change"
            assert second_payload["playback_id"] == first_payload["playback_id"]
            assert second_payload["version"] == first_payload["version"]
            assert second_payload["song_id"] == "song-1"


async def test_pubsub_relay_updates_pointer_cache_so_now_playing_reflects_it_immediately() -> (
    None
):
    """Criterion #3: a received pub/sub pointer updates `app.state.pointer_cache` (the
    `invalidate()`/`set()` seam) so `/now-playing` serves the new pointer right away —
    not after the process-local cache's TTL happens to expire."""
    redis = _FakeRedis()
    async with _running_app(redis) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=1, playback_id="pb-1"))
        resp = await client.get("/now-playing")
        assert resp.status_code == 200
        assert resp.json()["song_id"] == "song-1"

        # No live SSE client is required for the broadcaster to update the cache — the
        # relay runs on the app's own startup subscription regardless of listeners.
        next_record = _record(song_id="song-2", version=2, playback_id="pb-2")
        await redis.publish(CHANNEL, next_record.to_json())
        # Yield control so the broadcaster's background listener task (scheduled at
        # app startup) gets a turn to process the queued pub/sub message.
        for _ in range(5):
            await asyncio.sleep(0)

        resp2 = await client.get("/now-playing")
        assert resp2.status_code == 200
        assert resp2.json()["song_id"] == "song-2"
        assert resp2.json()["version"] == 2


async def test_events_idle_then_song_change_delivered_on_the_same_open_connection() -> None:
    """A listener who connects while the station is idle must receive the first
    `song-change` on that SAME already-open stream once a pointer is published — not
    only after reconnecting (criteria #1/#4: the push, not a reconnect, catches them up)."""
    redis = _FakeRedis()
    async with _running_app(redis) as (_app, client):
        async with client.stream("GET", "/events") as resp:
            events_iter = _sse_events(resp)
            name, payload = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert name == "idle"
            assert payload == {"status": "idle"}

            published = _record(song_id="song-9", version=9, playback_id="pb-9")
            await redis.publish(CHANNEL, published.to_json())

            name2, payload2 = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert name2 == "song-change"
            assert payload2["song_id"] == "song-9"
            assert payload2["version"] == 9
            assert payload2["playback_id"] == "pb-9"


async def test_events_unregisters_client_on_disconnect() -> None:
    """When an SSE client disconnects, its queue is unregistered (the generator's
    `finally`) so neither the queue set nor the `sse_connected_listeners` gauge leaks —
    the gauge whose flatness-vs-advances is the datastore-decoupling proof."""
    redis = _FakeRedis()
    async with _running_app(redis, heartbeat_seconds=0.05) as (app, client):
        app.state.pointer_cache.set(_record(song_id="song-1", version=1, playback_id="pb-1"))
        broadcaster = app.state.pointer_broadcaster
        async with client.stream("GET", "/events") as resp:
            events_iter = _sse_events(resp)
            await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert len(broadcaster._queues) == 1

        # After the stream closes, the server-side generator's `finally` runs
        # `unregister`; poll briefly since that cleanup is asynchronous to this side.
        for _ in range(200):
            if len(broadcaster._queues) == 0:
                break
            await asyncio.sleep(0.01)
        assert len(broadcaster._queues) == 0



# ── Per-user job-failed notifications (issue #16, acceptance criterion A4) ────────
#
# `/events` is also where a user's own `job-failed` notification rides (design D4: no
# new endpoint). `create_app()`'s `app.state.user_event_broadcaster` and the route's
# identity-based registration/merge into the SSE stream don't exist yet (issue #16
# phase 3) -- referenced only inside this test function (mirrors this file's own NOTE
# convention above) so a missing symbol fails only this test, not collection.


async def test_events_emits_a_users_job_failed_frame_alongside_song_change() -> None:
    """Acceptance A4: a `job-failed` notification published for the connecting
    client's own (cookie) identity is delivered as a `job-failed` SSE frame on the
    SAME `/events` connection that also carries `song-change`. Currently RED (issue
    #16 phase 3): `app.state.user_event_broadcaster` doesn't exist yet."""
    from songforge.web.identity import mint, sign

    redis = _FakeRedis()
    async with _running_app(redis) as (app, client):
        assert hasattr(app.state, "user_event_broadcaster"), (
            "issue #16 phase 3: create_app() must construct a UserEventBroadcaster "
            "on app.state (mirrors app.state.pointer_broadcaster) and /events must "
            "register the caller's identity with it, merging job-failed frames into "
            "the same SSE stream as song-change"
        )

        from songforge.config import get_settings

        user_id = mint()
        signed = sign(user_id, secret=get_settings().session_secret)
        client.cookies.set(get_settings().identity_cookie_name, signed)

        async with client.stream("GET", "/events") as resp:
            events_iter = _sse_events(resp)
            await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)  # idle/song-change

            from songforge.radio.user_events import user_channel

            await redis.publish(
                user_channel(get_settings().user_events_channel_prefix, user_id),
                '{"event": "job-failed", "job_id": "job-1"}',
            )

            name, payload = await asyncio.wait_for(events_iter.__anext__(), timeout=2.0)
            assert name == "job-failed"
            assert payload["job_id"] == "job-1"


async def test_metrics_endpoint_exposes_the_new_sse_metrics() -> None:
    """The three issue-#10 metrics are registered and scrapeable at `/metrics` — the
    observable surface for reasoning about listener load vs. datastore load (spec #78)."""
    redis = _FakeRedis()
    async with _running_app(redis) as (_app, client):
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        body = resp.text
        assert "songforge_sse_connected_listeners" in body
        assert "songforge_radio_pointer_events_published_total" in body
        assert "songforge_radio_pointer_events_relayed_total" in body
