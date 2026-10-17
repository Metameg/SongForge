"""partial index on jobs.state for non-terminal states

Revision ID: 0009_jobs_state_active_index
Revises: 0008_album_cover_path
Create Date: 2026-10-17

The worker's ``songforge_jobs_in_state`` gauge counts only non-terminal jobs. Indexing
just those rows keeps that count proportional to in-flight work instead of every job ever
created (a full ``GROUP BY state`` over ``jobs`` grew with history).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_jobs_state_active_index"
down_revision: str | None = "0008_album_cover_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_jobs_state_active",
        "jobs",
        ["state"],
        postgresql_where=sa.text(
            "state IN ('QUEUED', 'SUBMITTING', 'WAITING_FOR_WEBHOOK', 'INGEST_PENDING')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_state_active", table_name="jobs")
