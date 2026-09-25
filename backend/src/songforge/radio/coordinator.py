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

Issue #10 adds one more, in the same best-effort block: after the ``redis.set``, also
``redis.publish`` the same resolved view to the pub/sub channel each app instance's
``PointerBroadcaster`` subscribes to and relays to its ``/events`` SSE listeners
(criterion #2), so a connected listener is pushed the change instead of waiting on a
poll. Exactly mirrors the ``set`` contract -- a lost CAS race or
a no-op initialize never publishes (nothing changed to announce), and a publish failure
is caught and logged, never raised.

Issue #8, phase 3 (green) implementation. Issue #9 pointer-write addition. Issue #10
pub/sub publish addition.
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
from songforge.metrics import (
    radio_advances_total,
    radio_interrupts_total,
    radio_pointer_events_published_total,
)
from songforge.models import (
    RADIO_STATE_SINGLETON_ID,
    PlaybackQueue,
    RadioState,
    Song,
    Source,
    SOURCE_GENERATED,
    SOURCE_STATIC,
)
from songforge.radio.pointer_cache import PointerRecord
from songforge.radio.queue import pop_next_user_song
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
    """Best-effort write + publish of the resolved pointer view.

    Builds the same denormalized shape the web tier serves (`PointerRecord`, design
    D1) so a cold web instance can answer `/now-playing` with zero Postgres reads, sets
    it at the Redis pointer key (issue #9), then publishes the same payload to the
    pub/sub channel `/events` relays (issue #10, criterion #2). Any failure is logged
    and swallowed — Postgres has already committed by the time this runs, so a Redis
    outage must never fail the caller's init/advance. The try/except spans record
    construction too (not just the Redis calls), so even an unexpected error building
    the view can never turn a committed advance into a failed one.
    """
    settings = get_settings()
    try:
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
        payload = record.to_json()
        await redis.set(settings.radio_pointer_redis_key, payload)
        await redis.publish(settings.radio_pointer_channel, payload)
        radio_pointer_events_published_total.inc()
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

    # Issue #14 must-fix: static and generated songs share the `songs` table (spec
    # #48) -- the STATIC-candidate query must filter to `source == 'static'`, or a
    # generated song sitting in the catalog (already played once, or never queued at
    # all) could leak into the plain static rotation outside of ever being explicitly
    # queued and popped.
    songs = list(
        (await session.scalars(select(Song).where(Song.source == SOURCE_STATIC))).all()
    )
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

    Issue #14, criterion #2: a waiting user song takes priority over static filler --
    the user queue is peeked FIRST, and only an empty queue falls through to the
    static ``pick_static`` path below (unchanged from issue #8/#9/#10).
    """
    queue_row = await pop_next_user_song(session)
    if queue_row is not None:
        return await _advance_to_user_song(
            session, queue_row, expected_version=expected_version, redis=redis
        )

    settings = get_settings()
    # Issue #14 must-fix: see the matching comment in `initialize_if_absent` -- the
    # static-candidate query must filter to `source == 'static'` so a generated song
    # sitting in the catalog never leaks into the plain static rotation.
    songs = list(
        (await session.scalars(select(Song).where(Song.source == SOURCE_STATIC))).all()
    )
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


# ── Interrupt: a fresh user song cuts a currently-playing static filler ──────────
#
# Issue #14, criterion #3.
#
# `should_interrupt` is the PURE decision at the heart of criterion #3: a freshly-
# ready user song may interrupt the currently-playing song IFF the current pointer's
# `source` is `'static'` -- never a `'generated'` (user) song. This single guard gives
# both "never interrupts a user song" AND "at most once per static song" for free: an
# applied interrupt flips `source` to `'generated'`, so a subsequent wake finds
# `source != 'static'` and does not fire again until the NEXT static song is chosen at
# a boundary. Kept as a plain sync predicate (mirrors `radio.selection.pick_static`)
# so it is trivially unit-testable with no DB/session at all.
#
# `attempt_interrupt` is the async DB-wired counterpart `worker/radio_coordinator.py`
# calls on a `song_ready` LISTEN wake that is NOT the boundary timer: reads the current
# pointer, applies `should_interrupt`, and -- only when both that guard AND a non-empty
# user queue hold -- performs the same version-CAS `advance` uses (same
# `expected_version` convention: the coordinator's own prior read, never client-
# supplied), setting `source='generated'`, `started_at=now()`, and consuming the
# popped `PlaybackQueue` row ONLY on an applied CAS (a lost race leaves it unconsumed,
# mirroring `advance`'s own queue-consumption contract). Never touches
# `history.record` -- anti-repeat is a static-only concern (mirrors `advance`'s
# generated-song branch). `redis` is optional/best-effort, exactly like `advance`.


async def _advance_to_user_song(
    session: AsyncSession,
    queue_row: PlaybackQueue,
    *,
    expected_version: int,
    redis: Redis | None,
) -> bool:
    """CAS the pointer onto a queued user song (``source='generated'``) and, ONLY on
    an applied CAS, consume the queue row + best-effort publish. Shared by the
    boundary advance (criterion #2, ``advance``) and the off-boundary interrupt
    (criterion #3, ``attempt_interrupt``) -- both go through the exact same
    version-CAS, so a wake racing a boundary tick can never double-advance (whichever
    commits first wins; the other sees ``rowcount == 0``).

    On a lost CAS the queue row is left unconsumed for the winner (design D1/D4), and
    no static anti-repeat record is written -- a generated advance is not a
    static-repeat concern.
    """
    song = await session.get(Song, queue_row.song_id)
    if song is None:
        # The FK guarantees the song exists; defensively skip rather than crash the
        # coordinator loop on an unexpected inconsistency.
        log.error("radio_queue_row_missing_song", song_id=queue_row.song_id)
        return False

    settings = get_settings()
    duration = song.duration_seconds or settings.radio_default_track_seconds
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(seconds=duration)
    new_playback_id = str(uuid4())
    result = await session.execute(
        update(RadioState)
        .where(
            RadioState.id == RADIO_STATE_SINGLETON_ID,
            RadioState.version == expected_version,
        )
        .values(
            song_id=song.id,
            playback_id=new_playback_id,
            source=SOURCE_GENERATED,
            started_at=now,
            ends_at=ends_at,
            version=RadioState.version + 1,
        )
    )
    if cast("CursorResult[Any]", result).rowcount == 0:
        await session.commit()
        log.info("radio_advance_lost_race", expected_version=expected_version)
        return False

    # Consume the queue row ONLY now that the CAS is known applied (design D1/D4).
    queue_row.played_at = now
    await session.commit()
    radio_advances_total.inc()
    log.info(
        "radio_advanced_generated",
        song_id=song.id,
        playback_id=new_playback_id,
        version=expected_version + 1,
    )
    if redis is not None:
        await _write_pointer_best_effort(
            redis,
            song=song,
            playback_id=new_playback_id,
            started_at=now,
            ends_at=ends_at,
            version=expected_version + 1,
        )
    return True


def should_interrupt(current_source: Source) -> bool:
    """Whether a freshly-ready user song may interrupt the currently-playing song.

    Criterion #3: only when the current pointer is playing a STATIC filler --
    interrupting another user's song is never allowed. See the module-level note
    above for how this single guard also yields "at most once per static song".
    """
    return current_source == SOURCE_STATIC


async def attempt_interrupt(
    session: AsyncSession,
    history: RecentHistoryStore,
    *,
    expected_version: int,
    redis: Redis | None = None,
) -> bool:
    """Interrupt the currently-playing STATIC song with the oldest queued user song,
    via the same version-CAS `advance` uses (criterion #3).

    Returns True only if an interrupt was actually applied (guard passed AND the CAS
    applied); False on any no-op (current song isn't static, the user queue is empty,
    or the CAS lost a race) -- the caller (`worker/radio_coordinator.py`'s wake
    handler) never double-advances or crashes on a False return, mirroring `advance`.

    ``history`` is accepted for signature symmetry with `advance` (both funnel through
    `_advance_to_user_song`, which never touches it -- anti-repeat is a static-only
    concern, design D4).
    """
    pointer = await session.get(RadioState, RADIO_STATE_SINGLETON_ID)
    if pointer is None or pointer.source is None or not should_interrupt(pointer.source):
        return False

    queue_row = await pop_next_user_song(session)
    if queue_row is None:
        return False

    interrupted = await _advance_to_user_song(
        session, queue_row, expected_version=expected_version, redis=redis
    )
    if interrupted:
        radio_interrupts_total.inc()
    return interrupted
