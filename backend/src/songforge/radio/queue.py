"""FIFO pop/enqueue port onto the ``playback_queue`` table (issue #14, criterion #1/#2).

Mirrors ``radio.history.RedisRecentHistoryStore``'s role: a small, focused module the
coordinator (``radio.coordinator.advance`` / ``attempt_interrupt``) calls into so its
own decision logic stays unit-testable against ``sqlite+aiosqlite``, without needing a
mock/port abstraction the way Redis-backed collaborators do -- ``playback_queue`` lives
in the SAME Postgres database as ``radio_state``/``songs``, so these functions just take
the caller's own ``AsyncSession`` directly (same pattern as querying ``Song`` in
``radio.coordinator``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.models import PlaybackQueue


async def enqueue_song(
    session: AsyncSession, song_id: str, job_id: str | None
) -> PlaybackQueue:
    """Stage a new FIFO ``playback_queue`` row for a READY song (criterion #1).

    Mirrors ``jobs.ingest._finalize_ready``'s "pure decision, caller commits"
    convention: this only ``session.add()``s the row -- the caller
    (``jobs.ingest._finalize_ready``) owns the final commit, exactly like the ``Song``
    insert it enqueues alongside. Idempotent re-enqueue on a crash-replay is the
    CALLER's job (a job_id pre-check before calling this -- see
    ``jobs.ingest._finalize_ready``), not this function's: it always inserts.
    """
    row = PlaybackQueue(song_id=song_id, job_id=job_id)
    session.add(row)
    return row


async def pop_next_user_song(session: AsyncSession) -> PlaybackQueue | None:
    """Peek the oldest unplayed ``playback_queue`` row (FIFO by ``id``), or ``None``
    if the queue is empty (criterion #2's "fall back to static when empty").

    Deliberately does NOT mark the row played -- the caller
    (``radio.coordinator.advance`` / ``attempt_interrupt``) only consumes it after a
    version-CAS actually applies (a lost race must leave the row poppable by whichever
    leader wins next). Uses ``.with_for_update()`` as cheap insurance against a
    concurrent pop, consistent with house style (``jobs.dispatch.claim_next_job``,
    ``jobs.ingest.claim_next_ingest_job``), even though the radio coordinator is
    single-leader so a genuine double-pop race is not expected in practice.
    """
    stmt = (
        select(PlaybackQueue)
        .where(PlaybackQueue.played_at.is_(None))
        .order_by(PlaybackQueue.id)
        .limit(1)
        .with_for_update()
    )
    return (await session.scalars(stmt)).first()
