"""Unit tests for the pointer cache (issue #9, criteria #2 + #4 — the core of #9).

Contract exercised here (**not yet implemented** — this is the RED phase; see
`.orchestrator/report-phase1-tests.md` for the full write-up) lives in the not-yet-
existing module `songforge.radio.pointer_cache`:

- `PointerRecord` — the JSON-serializable resolved `/now-playing` view *minus*
  `server_time` (design D1), built from a `NowPlayingView` via `.from_view()`, with
  `.to_json()` / `.from_json()` for the Redis string value and `.to_response()` mirroring
  `NowPlayingView.to_response()` for the HTTP body.
- `PointerCache` — a process-local, TTL-bounded holder of one `PointerRecord` (design
  D3): `.get()` returns the record only within the TTL, else `None`; `.set()` stores a
  fresh record (resets the TTL clock); `.invalidate()` forces the next `.get()` to miss.
  Takes an injectable `clock` callable so TTL expiry is deterministic in tests.
- `get_now_playing_cached(...)` — the D4 warm/fallback orchestration: fresh local cache
  -> Redis -> Postgres (+ best-effort Redis write-back on the PG-fallback path), which is
  the heart of criterion #2 (process-local cache warmed from Redis) and criterion #4
  (repeated reads must never touch Postgres once Redis/the local cache is populated).

These are fast unit tests: `_FakeRedis` is a minimal in-memory async double (get/set,
with injectable failures to simulate "Redis is down"), and `_SpyPgLoader` stands in for
`songforge.radio.state.get_now_playing` so tests can assert it was/was-not called,
per the PRD testing decision that "no PG read" is legitimately verified by asserting the
loader is not invoked (see CONTEXT.md "Conventions").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from songforge.radio.pointer_cache import (
    PointerCache,
    PointerRecord,
    get_now_playing_cached,
)
from songforge.radio.state import NowPlayingView

# Not sourced from config in this module — `get_now_playing_cached` takes the Redis
# key as a plain argument (config only via `config.py`, at the call site: the route
# reads `settings.radio_pointer_redis_key` and passes it in). Any literal works here.
TEST_REDIS_KEY = "radio:pointer:test"


def _make_view(*, song_id: str = "song-1", version: int = 3) -> NowPlayingView:
    started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    return NowPlayingView(
        song_id=song_id,
        title="Song One",
        source="static",
        object_key=f"audio/{song_id}.mp3",
        audio_url=f"http://cdn.test/audio/{song_id}.mp3",
        started_at=started_at,
        ends_at=started_at + timedelta(seconds=180),
        duration_seconds=180,
        playback_id="pb-1",
        version=version,
    )


class _FakeClock:
    """Deterministic stand-in for `time.monotonic`: advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeRedis:
    """Minimal in-memory async Redis double: a `str -> str` store, get/set only.

    `raise_on_get` / `raise_on_set` simulate "Redis is down" for the fallback tests
    (design D4: a Redis error must fall back to Postgres, never raise to the caller).
    """

    def __init__(
        self,
        *,
        raise_on_get: Exception | None = None,
        raise_on_set: Exception | None = None,
    ) -> None:
        self._store: dict[str, str] = {}
        self._raise_on_get = raise_on_get
        self._raise_on_set = raise_on_set
        self.get_calls = 0
        self.set_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.set_calls += 1
        if self._raise_on_set is not None:
            raise self._raise_on_set
        self._store[key] = value


class _SpyPgLoader:
    """Stand-in for `songforge.radio.state.get_now_playing`: records call count."""

    def __init__(self, view: NowPlayingView | None) -> None:
        self._view = view
        self.calls = 0

    async def __call__(self, session: AsyncSession) -> NowPlayingView | None:
        self.calls += 1
        return self._view


_FAKE_SESSION = cast(AsyncSession, object())


# ── PointerCache (D3: process-local TTL cache) ──────────────────────────────


def test_pointer_cache_miss_when_empty() -> None:
    cache = PointerCache(ttl_seconds=1.0, clock=_FakeClock())
    assert cache.get() is None


def test_pointer_cache_hit_immediately_after_set() -> None:
    cache = PointerCache(ttl_seconds=1.0, clock=_FakeClock())
    record = PointerRecord.from_view(_make_view())

    cache.set(record)

    assert cache.get() == record


def test_pointer_cache_expires_after_ttl() -> None:
    clock = _FakeClock()
    cache = PointerCache(ttl_seconds=1.0, clock=clock)
    cache.set(PointerRecord.from_view(_make_view()))

    clock.advance(1.001)

    assert cache.get() is None


def test_pointer_cache_still_fresh_just_under_ttl() -> None:
    clock = _FakeClock()
    cache = PointerCache(ttl_seconds=1.0, clock=clock)
    cache.set(PointerRecord.from_view(_make_view()))

    clock.advance(0.999)

    assert cache.get() is not None


def test_pointer_cache_invalidate_forces_a_miss() -> None:
    cache = PointerCache(ttl_seconds=10.0, clock=_FakeClock())
    cache.set(PointerRecord.from_view(_make_view()))

    cache.invalidate()

    assert cache.get() is None


# ── PointerRecord JSON round-trip (D1) ──────────────────────────────────────


def test_pointer_record_json_round_trip_preserves_fields() -> None:
    record = PointerRecord.from_view(_make_view(song_id="song-42", version=7))

    restored = PointerRecord.from_json(record.to_json())

    assert restored == record


def test_pointer_record_storage_excludes_server_time_but_response_adds_it() -> None:
    """D1: the stored JSON is the view *minus* `server_time`; `server_time` is only
    stamped on at response-shaping time, same as `NowPlayingView.to_response`."""
    record = PointerRecord.from_view(_make_view())
    assert "server_time" not in record.to_json()

    server_time = datetime.now(timezone.utc)
    body = record.to_response(server_time=server_time)
    assert body["status"] == "playing"
    assert body["song_id"] == record.song_id
    assert body["server_time"] is not None


# ── get_now_playing_cached: D4 warm/fallback order ──────────────────────────


async def test_fresh_local_cache_serves_without_touching_redis_or_postgres() -> None:
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    cache.set(PointerRecord.from_view(_make_view(song_id="cached-song")))
    redis = _FakeRedis()
    pg_loader = _SpyPgLoader(_make_view())

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result is not None
    assert result.song_id == "cached-song"
    assert redis.get_calls == 0
    assert pg_loader.calls == 0


async def test_repeated_reads_within_ttl_perform_zero_datastore_calls() -> None:
    """Criterion #4 invariant, at the cache layer: with Redis populated, N repeated
    reads within the freshness TTL -> 0 Redis calls, 0 Postgres loads."""
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis()
    await redis.set(TEST_REDIS_KEY, PointerRecord.from_view(_make_view()).to_json())
    pg_loader = _SpyPgLoader(_make_view())

    first = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )
    redis.get_calls = 0  # only count calls made AFTER the initial warm

    for _ in range(5):
        again = await get_now_playing_cached(
            cache=cache,
            redis=redis,
            session=_FAKE_SESSION,
            pg_loader=pg_loader,
            redis_key=TEST_REDIS_KEY,
        )
        assert again == first

    assert redis.get_calls == 0
    assert pg_loader.calls == 0


async def test_redis_hit_on_a_cold_cache_populates_local_cache_and_skips_postgres() -> None:
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis()
    seeded = PointerRecord.from_view(_make_view(song_id="from-redis"))
    await redis.set(TEST_REDIS_KEY, seeded.to_json())
    pg_loader = _SpyPgLoader(_make_view())

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result == seeded
    assert pg_loader.calls == 0
    assert cache.get() == seeded  # local cache now warmed from Redis


async def test_redis_miss_falls_back_to_postgres_and_writes_back_to_redis() -> None:
    """D4 step 3: empty Redis -> PG fallback, populate the local cache, and
    best-effort write the resolved view back to Redis (cold-start warming, PRD #68)."""
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis()  # empty
    pg_loader = _SpyPgLoader(_make_view(song_id="from-pg"))

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result is not None
    assert result.song_id == "from-pg"
    assert pg_loader.calls == 1
    assert cache.get() is not None
    assert TEST_REDIS_KEY in redis._store
    assert PointerRecord.from_json(redis._store[TEST_REDIS_KEY]).song_id == "from-pg"


async def test_redis_error_on_read_falls_back_to_postgres_without_raising() -> None:
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis(raise_on_get=ConnectionError("redis down"))
    pg_loader = _SpyPgLoader(_make_view(song_id="from-pg-after-redis-error"))

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result is not None
    assert result.song_id == "from-pg-after-redis-error"
    assert pg_loader.calls == 1


async def test_redis_write_back_failure_after_pg_fallback_is_best_effort() -> None:
    """A write-back failure must not raise nor block returning the PG-sourced record
    (spec: Redis is derived/rebuildable, every Redis write is best-effort)."""
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis(raise_on_set=ConnectionError("redis down"))
    pg_loader = _SpyPgLoader(_make_view(song_id="from-pg-writeback-fails"))

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result is not None
    assert result.song_id == "from-pg-writeback-fails"


async def test_postgres_also_empty_returns_none_idle() -> None:
    """D4 step 4: Redis empty and Postgres has no playable pointer -> idle (None),
    unchanged from today's `get_now_playing() is None` -> 503 behavior."""
    cache = PointerCache(ttl_seconds=5.0, clock=_FakeClock())
    redis = _FakeRedis()
    pg_loader = _SpyPgLoader(None)

    result = await get_now_playing_cached(
        cache=cache,
        redis=redis,
        session=_FAKE_SESSION,
        pg_loader=pg_loader,
        redis_key=TEST_REDIS_KEY,
    )

    assert result is None
