"""radio_state pointer

Revision ID: 0002_radio_state
Revises: 0001_initial_songs
Create Date: 2026-09-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_radio_state"
down_revision: str | None = "0001_initial_songs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "radio_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "song_id",
            sa.String(length=64),
            sa.ForeignKey("songs.id"),
            nullable=True,
        ),
        sa.Column("playback_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("radio_state")
