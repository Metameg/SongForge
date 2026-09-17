"""Redis pointer cache + process-local warming (issue #9, criteria #2 + #4).

Decouples `/now-playing` reads from Postgres. The coordinator (`radio/coordinator.py`)
is the single writer of a resolved, denormalized copy of the pointer into a Redis
string key (design D1/D2); this module is the *reader* side:

- `PointerRecord` — the JSON-serializable resolved view (the `NowPlayingView` wire
  shape minus `server_time`, design D1), with `to_json`/`from_json` for the Redis
  value and `to_response` mirroring `NowPlayingView.to_response` for the HTTP body.
- `PointerCache` — a process-local, TTL-bounded holder of one `PointerRecord`
  (design D3), so repeated requests within the TTL cost zero datastore reads.
- `get_now_playing_cached` — the design D4 warm/fallback chain: fresh local cache ->
  Redis -> Postgres, with best-effort write-back to Redis on the Postgres path (PRD
  story #68, "cold-start warming"). This is the heart of criterion #2 (a process-local
  cache warmed from Redis) and criterion #4 (repeated reads never touch Postgres once
  Redis/the local cache is warm).

Every Redis operation here that sits on the read path is wrapped in a best-effort
try/except by `get_now_playing_cached` itself (not by the thin I/O helpers) — a Redis
outage must fall back to Postgres, never surface as a 500 (spec: Redis is
derived/rebuildable, Postgres wins on divergence).
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.logging_setup import get_logger
from songforge.metrics import radio_now_playing_source_total
from songforge.models import Source
from songforge.radio.state import NowPlayingView, _isoformat_utc

log = get_logger(__name__)

PgLoader = Callable[[AsyncSession], Awaitable["NowPlayingView | None"]]


@dataclass(frozen=True)
class PointerRecord:
    """The resolved `/now-playing` view, minus `server_time` (design D1).

    Stores everything the web tier needs to answer `/now-playing` with zero Postgres
    reads: song metadata (`title`, `object_key`, `audio_url`, `duration_seconds`) and
    the pointer (`started_at`/`ends_at`/`playback_id`/`version`). `server_time` is
    deliberately excluded from storage — it is stamped on fresh at response time
    (`to_response`), same as `NowPlayingView.to_response`, since a cached `server_time`
    would be stale the instant it was read back.
    """

    song_id: str
    title: str
    source: Source
    object_key: str
    audio_url: str
    started_at: str
    ends_at: str
    duration_seconds: int | None
    playback_id: str
    version: int

    def to_response(self, *, server_time: datetime) -> dict[str, Any]:
        """Shape this record into the `/now-playing` JSON body.

        Mirrors `NowPlayingView.to_response` exactly (same keys, same "playing"
        discriminator) so the route's wire contract is identical regardless of which
        layer (local cache / Redis / Postgres) resolved the read.
        """
        return {
            "status": "playing",
            "song_id": self.song_id,
            "title": self.title,
            "source": self.source,
            "object_key": self.object_key,
            "audio_url": self.audio_url,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "duration": self.duration_seconds,
            "playback_id": self.playback_id,
            "version": self.version,
            "server_time": _isoformat_utc(server_time),
        }

    def to_json(self) -> str:
        """The Redis string value: this record's fields, `server_time` excluded."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> PointerRecord:
        return cls(**json.loads(raw))

    @classmethod
    def from_view(cls, view: NowPlayingView) -> PointerRecord:
        """Build a record from the Postgres read model, formatting timestamps with the
        same tz-aware UTC ISO rule as the HTTP wire contract (`_isoformat_utc`)."""
        return cls(
            song_id=view.song_id,
            title=view.title,
            source=view.source,
            object_key=view.object_key,
            audio_url=view.audio_url,
            started_at=_isoformat_utc(view.started_at),
            ends_at=_isoformat_utc(view.ends_at),
            duration_seconds=view.duration_seconds,
            playback_id=view.playback_id,
            version=view.version,
        )


class PointerCache:
    """Process-local, TTL-bounded holder of one `PointerRecord` (design D3).

    Uses `time.monotonic` by default (injectable for deterministic tests) — never wall
    clock, which can jump (NTP, DST) and corrupt expiry math. `invalidate()` is a seam
    for a future SSE/pub-sub song-change event to force an early miss; nothing wires it
    yet (out of scope for #9).
    """

    def __init__(
        self, *, ttl_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._record: PointerRecord | None = None
        self._set_at: float | None = None

    def get(self) -> PointerRecord | None:
        """The cached record if present and within the TTL, else `None`."""
        if self._record is None or self._set_at is None:
            return None
        if self._clock() - self._set_at >= self._ttl_seconds:
            return None
        return self._record

    def set(self, record: PointerRecord) -> None:
        """Store `record`, resetting the TTL clock."""
        self._record = record
        self._set_at = self._clock()

    def invalidate(self) -> None:
        """Force the next `get()` to miss, regardless of the TTL."""
        self._record = None
        self._set_at = None


async def read_pointer_from_redis(redis: Redis, *, key: str) -> PointerRecord | None:
    """`GET key` -> the stored record, or `None` if the key is absent.

    Exceptions propagate — best-effort handling belongs at the call site
    (`get_now_playing_cached`), matching this codebase's Redis convention.
    """
    raw = await redis.get(key)
    if raw is None:
        return None
    # `decode_responses=True` on the shared client means this is already `str` at
    # runtime; the redis-py stubs are generic over bytes|str, so normalize explicitly
    # rather than asserting the narrower type (matches `radio/history.py`'s convention).
    text = raw if isinstance(raw, str) else raw.decode()
    return PointerRecord.from_json(text)


async def write_pointer_to_redis(redis: Redis, record: PointerRecord, *, key: str) -> None:
    """`SET key <json>` — overwrite the pointer key with `record`'s resolved view.

    Exceptions propagate; the caller wraps this in a best-effort try/except.
    """
    await redis.set(key, record.to_json())


async def get_now_playing_cached(
    *,
    cache: PointerCache,
    redis: Redis,
    session: AsyncSession,
    pg_loader: PgLoader,
    redis_key: str,
) -> PointerRecord | None:
    """The design D4 warm/fallback chain: local cache -> Redis -> Postgres.

    1. A fresh local entry serves the request with zero datastore calls.
    2. Else, read the Redis pointer key; a hit populates the local cache and returns
       (zero Postgres reads — criterion #4). A Redis error is logged and treated the
       same as a miss (never raised to the caller).
    3. Else, fall back to Postgres (`pg_loader`), populate the local cache, and
       best-effort write the resolved record back to Redis so the next cold instance
       (or a cold-start herd) is absorbed by Redis instead of Postgres (PRD story #68).
       A write-back failure is logged and ignored.
    4. Postgres also has no playable pointer -> `None` (the caller's idle path).
    """
    local = cache.get()
    if local is not None:
        radio_now_playing_source_total.labels(source="local").inc()
        return local

    try:
        redis_record = await read_pointer_from_redis(redis, key=redis_key)
    except Exception:
        log.warning("radio_pointer_read_failed", exc_info=True)
        redis_record = None

    if redis_record is not None:
        cache.set(redis_record)
        radio_now_playing_source_total.labels(source="redis").inc()
        return redis_record

    view = await pg_loader(session)
    radio_now_playing_source_total.labels(source="postgres").inc()
    if view is None:
        return None

    record = PointerRecord.from_view(view)
    cache.set(record)

    try:
        await write_pointer_to_redis(redis, record, key=redis_key)
    except Exception:
        log.warning("radio_pointer_writeback_failed", song_id=record.song_id, exc_info=True)

    return record
