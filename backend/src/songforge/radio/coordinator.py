"""Single-leader radio coordinator: initialize-if-absent + version-CAS advance.

Satisfies issue #8 criteria #1 (pointer), #2 (advance + anti-repeat + never-empty), and
#5 (a listener's song changes at the boundary — the client re-fetches ``/now-playing``
once this module's CAS has moved the pointer). Leadership (a Postgres advisory lock) and
the clock-driven ``sleep(ends_at - now)`` timer live in ``worker/main.py``; this module
holds only the pointer-mutation *logic*, kept pure enough of Redis to unit-test against
in-memory SQLite (``sqlite+aiosqlite``) with a stubbed ``RecentHistoryStore``, per the PRD
testing decision to keep the coordinator's decision logic on the fast unit path.

Issue #9 adds one more responsibility: after a *genuine* success (a real initialize or
an applied CAS, never a lost race or a no-op), best-effort write the fully-resolved
pointer view to the Redis pointer key (design D2) so the web tier can serve
``/now-playing`` from Redis instead of Postgres (criteria #2/#4 of issue #9). The write
is wrapped in its own try/except: Postgres has already committed by that point, so a
Redis outage must never turn a successful advance into a failed one.

Issue #8, phase 3 (green) implementation. Issue #9 pointer-write addition.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.config import get_settings
from songforge.logging_setup import get_logger
from songforge.metrics import radio_advances_total
from songforge.models import RADIO_STATE_SINGLETON_ID, RadioState, Song, SOURCE_STATIC
from songforge.radio.pointer_cache import PointerRecord
from songforge.radio.selection import pick_static
from songforge.radio.state import NowPlayingView
from songforge.storage import get_storage

log = get_logger(__name__)


async def _write_pointer_best_effort(
    redis: Redis,
    *,
    song: Song,
    playback_id: str,
    started_at: datetime,
    ends_at: datetime,
    version: int,
) -> None:
    """Best-effort write of the resolved pointer view to the Redis pointer key.

    Builds the same denormalized shape the web tier serves (`PointerRecord`, design
    D1) so a cold web instance can answer `/now-playing` with zero Postgres reads. Any
    failure is logged and swallowed — Postgres has already committed by the time this
    runs, so a Redis outage must never fail the caller's init/advance.
    """
    settings = get_settings()
    record = PointerRecord.from_view(
        NowPlayingView(
            song_id=song.id,
            title=song.title,
            source=song.source,
            object_key=song.object_key,
            audio_url=get_storage().public_url(song.object_key),
            started_at=started_at,
            ends_at=ends_at,
            duration_seconds=song.duration_seconds,
            playback_id=playback_id,
            version=version,
        )
    )
    try:
        await redis.set(settings.radio_pointer_redis_key, record.to_json())
    except Exception:
        log.warning("radio_pointer_write_failed", song_id=song.id, exc_info=True)


class RecentHistoryStore(Protocol):
    """Port onto the Redis capped list of recently-played static song ids.

    A real implementation backs this with a Redis list (LPUSH + LTRIM) per the spec's
    "hot anti-repeat reads come from a Redis capped list"; tests substitute a trivial
    in-memory stub so ``advance``/``initialize_if_absent`` are unit-testable without a
    live Redis.
    """

    async def recent_ids(self) -> set[str]:
        """Currently "recent" static song ids to avoid when picking the next song."""
        ...

    async def record(self, song_id: str) -> None:
        """Record ``song_id`` as just-played (pushed onto the capped recent list)."""
        ...


async def initialize_if_absent(
    session: AsyncSession, history: RecentHistoryStore, redis: Redis | None = None
) -> bool:
    """Create the ``radio_state`` pointer (``version=0``) if none exists yet.

    - No pointer, static songs exist -> ``pick_static`` an initial song, insert the
      pointer at ``version=0``, return True.
    - No pointer, no static songs at all -> do nothing (idle-until-content); return
      False. The caller (worker) logs and retries later rather than erroring.
    - A pointer already exists -> no-op; return False. Cold-start resume (recomputing
      remaining time from ``ends_at``, or advancing if already past) is the caller's job.

    ``redis`` is optional and backward-compatible (issue #9, design D2): when given, a
    genuine initialize (not the already-exists no-op) best-effort writes the resolved
    pointer view to the Redis pointer key after the Postgres commit.
    """
    existing = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)
    if existing is not None:
        return False

    songs = list((await session.scalars(select(Song))).all())
    if not songs:
        log.info("radio_library_empty")
        return False

    settings = get_settings()
    recent_ids = await history.recent_ids()
    song_by_id = {s.id: s for s in songs}
    song_id = pick_static(list(song_by_id), recent_ids, current_id=None)
    duration = song_by_id[song_id].duration_seconds or settings.radio_default_track_seconds

    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(seconds=duration)
    playback_id = str(uuid4())
    pointer = RadioState(
        id=RADIO_STATE_SINGLETON_ID,
        song_id=song_id,
        playback_id=playback_id,
        source=SOURCE_STATIC,
        started_at=now,
        ends_at=ends_at,
        version=0,
    )
    session.add(pointer)
    await session.commit()
    await history.record(song_id)
    log.info("radio_initialized", song_id=song_id, version=0)
    if redis is not None:
        await _write_pointer_best_effort(
            redis,
            song=song_by_id[song_id],
            playback_id=playback_id,
            started_at=now,
            ends_at=ends_at,
            version=0,
        )
    return True


async def advance(
    session: AsyncSession,
    history: RecentHistoryStore,
    *,
    expected_version: int,
    redis: Redis | None = None,
) -> bool:
    """Advance the pointer to the next static song via version-CAS (criteria #2, #5).

    Picks the next song with ``songforge.radio.selection.pick_static`` against the
    catalog and the current recent-history set, then applies:

        UPDATE radio_state
           SET song_id=:next, playback_id=:new_id, source='static',
               started_at=now(), ends_at=now()+:duration, version=version+1
         WHERE id=1 AND version=:expected_version

    ``expected_version`` must be the coordinator's own prior read, never client-supplied.
    Returns True if the CAS applied (this call performed the advance and recorded the new
    song via ``history.record``); False if it matched 0 rows because another instance had
    already advanced first (lost the race) — the caller backs off without double-advancing.

    ``redis`` is optional and backward-compatible, keyword-only (issue #9, design D2):
    when given, an *applied* CAS (never a lost race) best-effort writes the resolved
    pointer view to the Redis pointer key after the Postgres commit.
    """
    settings = get_settings()
    songs = list((await session.scalars(select(Song))).all())
    if not songs:
        log.info("radio_library_empty")
        return False

    current = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)
    current_song_id = current.song_id if current is not None else None

    recent_ids = await history.recent_ids()
    song_by_id = {s.id: s for s in songs}
    next_song_id = pick_static(list(song_by_id), recent_ids, current_id=current_song_id)
    duration = (
        song_by_id[next_song_id].duration_seconds or settings.radio_default_track_seconds
    )

    now = datetime.now(timezone.utc)
    new_playback_id = str(uuid4())
    result = await session.execute(
        update(RadioState)
        .where(
            RadioState.id == RADIO_STATE_SINGLETON_ID,
            RadioState.version == expected_version,
        )
        .values(
            song_id=next_song_id,
            playback_id=new_playback_id,
            source=SOURCE_STATIC,
            started_at=now,
            ends_at=now + timedelta(seconds=duration),
            version=RadioState.version + 1,
        )
    )
    if cast("CursorResult[Any]", result).rowcount == 0:
        await session.commit()
        log.info("radio_advance_lost_race", expected_version=expected_version)
        return False

    await session.commit()
    await history.record(next_song_id)
    radio_advances_total.inc()
    log.info(
        "radio_advanced",
        song_id=next_song_id,
        playback_id=new_playback_id,
        version=expected_version + 1,
    )
    if redis is not None:
        await _write_pointer_best_effort(
            redis,
            song=song_by_id[next_song_id],
            playback_id=new_playback_id,
            started_at=now,
            ends_at=now + timedelta(seconds=duration),
            version=expected_version + 1,
        )
    return True
