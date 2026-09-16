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

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
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
