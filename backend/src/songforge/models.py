"""SQLAlchemy ORM models — Postgres is the source of truth.

This scaffold defines the ``songs`` catalog, which the static library is seeded into and
which later tickets extend (generated songs land here too, so the radio treats static and
generated audio as indistinguishable playable objects — spec #48). ``radio_state`` is the
single-row pointer onto the shared server timeline (issue #8, criterion #1). Users,
generation jobs, and playback history are added by their own tickets.

Column types are kept portable (no Postgres-only types) so seed/radio logic can be
unit-tested against in-memory SQLite without a live database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Whether a song came from the curated static library or was user-generated. Stored as a
# portable string; typed as a Literal so callers get checking instead of bare strings.
Source = Literal["static", "generated"]
SOURCE_STATIC: Source = "static"
SOURCE_GENERATED: Source = "generated"


class Base(DeclarativeBase):
    pass


class Song(Base):
    """A playable audio object in the catalog (static-library or user-generated)."""

    __tablename__ = "songs"

    # Own UUID/stem string PK — the row we can always find.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'static' | 'generated' — explicit String column keeps it portable; the Mapped
    # Literal gives type-checked reads/writes without a DB-level enum.
    source: Mapped[Source] = mapped_column(String(16), nullable=False)
    # Immutable object-storage key (audio/<id>.mp3).
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Fixed single-row id (spec convention: "id = 1, always one row").
RADIO_STATE_SINGLETON_ID = 1


class RadioState(Base):
    """Single-row pointer onto the shared server timeline (issue #8, criterion #1).

    ``song_id``/``playback_id``/``source``/``started_at``/``ends_at`` are nullable only
    for the not-yet-initialized/idle state (no static library seeded yet); once the
    coordinator initializes the pointer they are always set together. ``version`` is the
    monotonically increasing CAS guard the coordinator's advance step relies on to avoid
    a double-advance (see ``songforge.radio.coordinator.advance``).
    """

    __tablename__ = "radio_state"
    # Enforce the single-pointer-of-record invariant at the DB, not just by convention:
    # the coordinator only ever touches ``RADIO_STATE_SINGLETON_ID``, so any other row
    # would be silently ignored while corrupting "one pointer" (see the model docstring).
    __table_args__ = (CheckConstraint("id = 1", name="ck_radio_state_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    song_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("songs.id"), nullable=True
    )
    playback_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source: Mapped[Source | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# Generation job state machine (issue #12; PRD §Generation job pipeline):
#
#   QUEUED -> SUBMITTING -> WAITING_FOR_WEBHOOK -> INGEST_PENDING -> READY
#                       \\_______________ FAILED ______________/
#
# Issue #12 drives QUEUED -> SUBMITTING -> WAITING_FOR_WEBHOOK (plus requeue to QUEUED,
# or straight to FAILED on a terminal 4xx). Issue #13 drives WAITING_FOR_WEBHOOK ->
# INGEST_PENDING (the webhook handler, songforge.web.routes.webhook) and
# INGEST_PENDING -> READY / FAILED (the async ingest worker, songforge.jobs.ingest).
# Playback-queue enqueue on READY (issue #14, criterion #1) -- see PlaybackQueue below.
JobState = Literal[
    "QUEUED",
    "SUBMITTING",
    "WAITING_FOR_WEBHOOK",
    "INGEST_PENDING",
    "READY",
    "FAILED",
]
JOB_STATE_QUEUED: JobState = "QUEUED"
JOB_STATE_SUBMITTING: JobState = "SUBMITTING"
JOB_STATE_WAITING_FOR_WEBHOOK: JobState = "WAITING_FOR_WEBHOOK"
JOB_STATE_INGEST_PENDING: JobState = "INGEST_PENDING"
JOB_STATE_READY: JobState = "READY"
JOB_STATE_FAILED: JobState = "FAILED"

# States that still hold a semaphore slot ("in-flight" against generation concurrency).
# jobs/dispatch.py's 429 reconcile path counts exactly these rows to snap the Redis
# global counter back to Postgres truth (see .orchestrator/CONTEXT.md "In-scope
# decision"). QUEUED (not yet dispatched) and the terminal states (READY/FAILED) never
# hold a slot.
ACTIVE_JOB_STATES: tuple[JobState, ...] = (
    JOB_STATE_SUBMITTING,
    JOB_STATE_WAITING_FOR_WEBHOOK,
    JOB_STATE_INGEST_PENDING,
)


class Job(Base):
    """A generation job: one prompt submission through to a playable song (issue #12).

    ``job_id`` is our own UUID hex string PK -- the row we can always find by handle.
    ``seq`` is the separate FIFO ordering key dispatch claims by (``FOR UPDATE SKIP
    LOCKED ORDER BY seq``, criterion #2): a Postgres ``Identity`` column so it is
    populated server-side and strictly monotonic, backing the partial index
    ``(seq) WHERE state='QUEUED'`` added in migration ``0003_jobs`` (an O(log n) claim
    with no sort, rather than ``ORDER BY created_at``).

    Unlike the rest of this module, ``seq`` is deliberately NOT portable to SQLite
    ``create_all``: SQLite has no server-side generator for a non-PK identity column
    (confirmed experimentally -- inserting a ``Job`` without an explicit ``seq`` raises
    a NOT NULL violation on SQLite even though the identical insert works against real
    Postgres). Edge tests that don't care about a genuine generated ``seq`` value
    install a test-only ``before_insert`` shim (see ``tests/test_create_route.py``);
    tests that must observe genuine Postgres-generated ordering
    (``tests/test_jobs_queue_integration.py``) run against real Postgres, marked
    ``@pytest.mark.integration``.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[JobState] = mapped_column(
        String(32), nullable=False, default=JOB_STATE_QUEUED
    )

    # External handles (nullable, filled after a successful submit -- criterion #4).
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversion_id_1: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversion_id_2: Mapped[str | None] = mapped_column(String(64), nullable=True)
    eta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credit_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Requeue/backoff bookkeeping: claim skips rows whose available_at is in the future
    # (a 429/5xx/timeout requeue sets it ahead; see songforge.jobs.dispatch).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # The callback URL sent to the generation API at submit time (config-derived;
    # the receiving webhook handler is a later issue -- see CONTEXT.md scope).
    webhook_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Webhook-recorded metadata (issue #13, acceptance criterion #1): `audio_url` is a
    # *hint* URL that can expire before ingest runs -- the ingest worker refreshes it
    # via a by-id lookup when needed (see songforge.jobs.ingest). Overwritten in place
    # on refresh, never a history of URLs.
    audio_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    audio_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set at READY (issue #13, criterion #3): the playable Song this job produced.
    # `Song.id` is the job's `conversion_id_1` (the canonical conversion the PRD says
    # to store), not a separately generated id -- see songforge.jobs.ingest.
    song_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("songs.id"), nullable=True
    )

    # Ingest retry bookkeeping (issue #13): a SEPARATE counter from `attempts` above --
    # commingling them would erase whether a retry happened during dispatch or during
    # ingest. `available_at` (already defined above) is reused as-is for ingest's
    # requeue backoff (the job's `state` alone disambiguates which stage it belongs to).
    ingest_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PlaybackQueue(Base):
    """FIFO queue of READY user songs awaiting air time (issue #14, criterion #1).

    ``id`` is the monotonic FIFO ordering key -- a Postgres ``Identity`` column, same
    convention as ``Job.seq`` (see that docstring): NOT portable to SQLite
    ``create_all``'s server-side generation, so unit tests that don't need a genuine
    generated id set it explicitly (mirrors ``tests/test_create_route.py``'s
    ``before_insert`` shim pattern), and tests that must observe genuine
    Postgres-generated FIFO ordering run against real Postgres
    (``@pytest.mark.integration``).

    ``played_at`` is NULL while the row is waiting its turn; it is set only when this
    row is popped onto the air by an APPLIED version-CAS (``radio.coordinator.advance``
    for a boundary pop, ``radio.coordinator.attempt_interrupt`` for a mid-song
    interrupt) -- never on a lost CAS race, so a lost-race row stays poppable by
    whichever leader wins next (see ``radio.queue.pop_next_user_song``).
    """

    __tablename__ = "playback_queue"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    song_id: Mapped[str] = mapped_column(String(64), ForeignKey("songs.id"), nullable=False)
    # Nullable: traceability back to the originating job when known, but the queue
    # itself only needs `song_id` to hand a playable song to the coordinator.
    job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("jobs.job_id"), nullable=True
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    played_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
