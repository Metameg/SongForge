"""FIFO pop/enqueue port onto the ``playback_queue`` table (issue #14, criterion #1/#2).

Mirrors ``radio.history.RedisRecentHistoryStore``'s role: a small, focused module the
coordinator (``radio.coordinator.advance`` / ``attempt_interrupt``) calls into so its
own decision logic stays unit-testable against ``sqlite+aiosqlite``, without needing a
mock/port abstraction the way Redis-backed collaborators do -- ``playback_queue`` lives
in the SAME Postgres database as ``radio_state``/``songs``, so these functions just take
the caller's own ``AsyncSession`` directly (same pattern as querying ``Song`` in
``radio.coordinator``). Also hosts the waiting-queue status read (issue #38).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from songforge.models import Job, PlaybackQueue


@dataclass(frozen=True)
class QueuePositionEntry:
    """One of the caller's own waiting jobs and its 1-based GLOBAL position in the FIFO."""

    job_id: str
    position: int


@dataclass(frozen=True)
class QueueStatus:
    """Waiting-queue snapshot: total ``depth`` plus the caller's own ``positions``."""

    depth: int
    positions: list[QueuePositionEntry]


async def get_queue_status(session: AsyncSession, user_id: str) -> QueueStatus:
    """Depth of the waiting FIFO (``played_at IS NULL``) and ``user_id``'s own positions.

    Order is ``playback_queue.id`` (the FIFO key ``pop_next_user_song`` also uses). Rank
    is computed over ALL waiting rows (a window function) *before* filtering to the
    caller, so a position is where the song sits in the whole queue. Rows with a null
    ``job_id`` count toward depth and rank but join to no job, so never appear in
    ``positions``.
    """
    waiting = (
        select(
            PlaybackQueue.job_id.label("job_id"),
            func.row_number().over(order_by=PlaybackQueue.id).label("pos"),
        )
        .where(PlaybackQueue.played_at.is_(None))
        .subquery()
    )
    depth = (
        await session.scalar(
            select(func.count())
            .select_from(PlaybackQueue)
            .where(PlaybackQueue.played_at.is_(None))
        )
    ) or 0
    rows = await session.execute(
        select(waiting.c.job_id, waiting.c.pos)
        .join(Job, Job.job_id == waiting.c.job_id)
        .where(Job.user_id == user_id)
        .order_by(waiting.c.pos)
    )
    return QueueStatus(
        depth=depth,
        positions=[QueuePositionEntry(job_id=str(j), position=int(p)) for j, p in rows.all()],
    )


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
