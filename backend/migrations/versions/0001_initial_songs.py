"""initial: songs catalog

Revision ID: 0001_initial_songs
Revises:
Create Date: 2026-09-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_songs"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "songs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("object_key", name="uq_songs_object_key"),
    )
    op.create_index("ix_songs_source", "songs", ["source"])


def downgrade() -> None:
    op.drop_index("ix_songs_source", table_name="songs")
    op.drop_table("songs")
