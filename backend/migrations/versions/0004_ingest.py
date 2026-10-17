"""webhook + async ingest columns

Revision ID: 0004_ingest
Revises: 0003_jobs
Create Date: 2026-09-19

Adds the columns the webhook handler and async ingest worker need (issue #13):
recorded webhook metadata (`audio_url` hint, `audio_duration`, `title`), the song
link (`song_id` FK -> `songs.id`, set at READY), and ingest retry bookkeeping
(`ingest_attempts` -- kept separate from dispatch's `attempts` so the two stages'
retry counts never commingle; `available_at` is reused as-is for ingest backoff,
since a job's `state` alone disambiguates which stage a given value belongs to).
`ix_jobs_ingest_pending_seq` mirrors `0003_jobs`'s `ix_jobs_queued_seq`: an O(log n)
partial index so the ingest claim query is an index scan, not a full sort.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_ingest"
down_revision: str | None = "0003_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("audio_url", sa.String(length=1024), nullable=True))
    op.add_column("jobs", sa.Column("audio_duration", sa.Float(), nullable=True))
    op.add_column("jobs", sa.Column("title", sa.String(length=255), nullable=True))
    # NOTE: `op.add_column()` does NOT create a FK constraint even if the given
    # `Column` carries an inline `ForeignKey` -- Alembic requires a separate
    # `create_foreign_key()` call for that (a well-known gotcha; see Alembic's
    # `add_column()` docs). Add the plain column, then the constraint explicitly.
    op.add_column("jobs", sa.Column("song_id", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        "fk_jobs_song_id_songs", "jobs", "songs", ["song_id"], ["id"]
    )
    op.add_column(
        "jobs",
        sa.Column("ingest_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_jobs_ingest_pending_seq",
        "jobs",
        ["seq"],
        postgresql_where=sa.text("state = 'INGEST_PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_ingest_pending_seq", table_name="jobs")
    op.drop_column("jobs", "ingest_attempts")
    op.drop_constraint("fk_jobs_song_id_songs", "jobs", type_="foreignkey")
    op.drop_column("jobs", "song_id")
    op.drop_column("jobs", "title")
    op.drop_column("jobs", "audio_duration")
    op.drop_column("jobs", "audio_url")
