"""jobs queue

Revision ID: 0003_jobs
Revises: 0002_radio_state
Create Date: 2026-09-18

Adds the durable generation-job queue (issue #12). `seq` is a Postgres `Identity`
bigint so it is populated server-side and strictly monotonic -- the FIFO claim
ordering key (`FOR UPDATE SKIP LOCKED ... ORDER BY seq`, criterion #2). The partial
index `ix_jobs_queued_seq` covers exactly the claim query's predicate (`state =
'QUEUED'`) so the claim is an O(log n) index-only scan instead of a full sort over
every job ever created.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_jobs"
down_revision: str | None = "0002_radio_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("lyrics", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("conversion_id_1", sa.String(length=64), nullable=True),
        sa.Column("conversion_id_2", sa.String(length=64), nullable=True),
        sa.Column("eta", sa.Integer(), nullable=True),
        sa.Column("credit_estimate", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("webhook_url", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    # FIFO claim seam (criterion #2): O(log n) index-only scan for the oldest QUEUED
    # row, instead of a full sort/scan over every job ever created.
    op.create_index(
        "ix_jobs_queued_seq",
        "jobs",
        ["seq"],
        postgresql_where=sa.text("state = 'QUEUED'"),
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_queued_seq", table_name="jobs")
    op.drop_index("ix_jobs_user_id", table_name="jobs")
    op.drop_table("jobs")
