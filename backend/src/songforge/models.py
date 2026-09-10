"""SQLAlchemy ORM models — Postgres is the source of truth.

This scaffold defines the ``songs`` catalog, which the static library is seeded into and
which later tickets extend (generated songs land here too, so the radio treats static and
generated audio as indistinguishable playable objects — spec #48). Users, generation
jobs, the ``radio_state`` pointer, and playback history are added by their own tickets.

Column types are kept portable (no Postgres-only types) so seed logic can be unit-tested
against in-memory SQLite without a live database.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SOURCE_STATIC = "static"
SOURCE_GENERATED = "generated"


class Base(DeclarativeBase):
    pass


class Song(Base):
    """A playable audio object in the catalog (static-library or user-generated)."""

    __tablename__ = "songs"

    # Own UUID/stem string PK — the row we can always find.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'static' | 'generated' — kept as a string for portability across DB engines.
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # Immutable object-storage key (audio/<id>.mp3).
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
