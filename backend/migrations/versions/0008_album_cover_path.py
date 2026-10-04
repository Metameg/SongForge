"""album_cover_path on songs and jobs

Revision ID: 0008_album_cover_path
Revises: 0007_correlation_id
Create Date: 2026-10-04

Adds nullable ``songs.album_cover_path`` and ``jobs.album_cover_path`` (issue #36). The Job
column carries the value webhook -> ingest (mirrors audio_url/title); the Song column is
what now-playing reads. Nullable so existing rows and static songs are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_album_cover_path"
down_revision: str | None = "0007_correlation_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("songs", sa.Column("album_cover_path", sa.String(length=512), nullable=True))
    op.add_column("jobs", sa.Column("album_cover_path", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "album_cover_path")
    op.drop_column("songs", "album_cover_path")
