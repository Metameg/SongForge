"""correlation id: end-to-end trace column on jobs

Revision ID: 0007_correlation_id
Revises: 0006_failure_recovery
Create Date: 2026-09-26

Adds `jobs.correlation_id` (issue #18, acceptance criterion #1): the submit-time
correlation ID persisted so dispatch/ingest/webhook/watchdog can each re-bind it for
their own log lines, threading one trace across the async pipeline's separate
process/request hops (see `.orchestrator/CONTEXT.md`). Nullable -- pre-existing rows,
and any caller that doesn't yet stamp it, must not break.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_correlation_id"
down_revision: str | None = "0006_failure_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("correlation_id", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "correlation_id")
