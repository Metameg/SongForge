"""failure recovery: watchdog bookkeeping + refund identity

Revision ID: 0006_failure_recovery
Revises: 0005_playback_queue
Create Date: 2026-09-24

Adds the columns issue #16's watchdog needs (see `.orchestrator/CONTEXT.md`):

- `client_ip` / `is_authenticated`: persisted at create time (`web/routes/create.py`)
  so the terminal-failure sweep can reconstruct the exact `Identity` + ip that created
  a job for an accurate `RateLimiter.refund` (an anon create charges both the cookie
  AND the IP counters; the job row previously only stored `user_id`).
- `failure_handled_at`: the row-claim guard making the refund+notify sweep idempotent
  -- NULL means a FAILED row still needs handling.

`ix_jobs_failed_unhandled` mirrors `0003_jobs`'s `ix_jobs_queued_seq` /
`0004_ingest`'s `ix_jobs_ingest_pending_seq`: a partial index so
`claim_terminal_failure_job`'s claim query is an index scan, not a full table scan.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_failure_recovery"
down_revision: str | None = "0005_playback_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("client_ip", sa.String(length=64), nullable=True))
    op.add_column(
        "jobs",
        sa.Column(
            "is_authenticated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "jobs", sa.Column("failure_handled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_jobs_failed_unhandled",
        "jobs",
        ["seq"],
        postgresql_where=sa.text("state = 'FAILED' AND failure_handled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_failed_unhandled", table_name="jobs")
    op.drop_column("jobs", "failure_handled_at")
    op.drop_column("jobs", "is_authenticated")
    op.drop_column("jobs", "client_ip")
