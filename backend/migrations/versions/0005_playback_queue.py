"""playback queue

Revision ID: 0005_playback_queue
Revises: 0004_ingest
Create Date: 2026-09-24

Adds the ``playback_queue`` table (issue #14, criterion #1): the FIFO queue of READY
user songs awaiting air time. ``id`` is a Postgres ``Identity`` column (the monotonic
FIFO ordering key, same convention as ``jobs.seq`` -- see that migration/model
docstring for the matching SQLite-portability caveat). ``played_at`` starts NULL and is
set only when a row is popped onto the air by an applied version-CAS (see
``songforge.radio.queue``/``songforge.radio.coordinator``).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_playback_queue"
down_revision: str | None = "0004_ingest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playback_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("song_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column(
            "enqueued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("played_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_playback_queue_song_id_songs",
        "playback_queue",
        "songs",
        ["song_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_playback_queue_job_id_jobs",
        "playback_queue",
        "jobs",
        ["job_id"],
        ["job_id"],
    )
    # Partial index mirroring `ix_jobs_queued_seq`/`ix_jobs_ingest_pending_seq`: the
    # coordinator's `pop_next_user_song` only ever scans unplayed rows, oldest first --
    # an O(log n) index scan instead of a full sort/filter.
    op.create_index(
        "ix_playback_queue_unplayed_id",
        "playback_queue",
        ["id"],
        postgresql_where=sa.text("played_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_playback_queue_unplayed_id", table_name="playback_queue")
    op.drop_constraint(
        "fk_playback_queue_job_id_jobs", "playback_queue", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_playback_queue_song_id_songs", "playback_queue", type_="foreignkey"
    )
    op.drop_table("playback_queue")
