"""Job runs, and when the pass behind each alert was acquired (Phase 10).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "aoi_id", sa.Integer(), sa.ForeignKey("aois.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("started_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_utc", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("passes_read", sa.Integer(), nullable=False),
        sa.Column("passes_failed", sa.Integer(), nullable=False),
        sa.Column("fallback_passes", sa.Integer(), nullable=False),
        sa.Column("bytes_read", sa.BigInteger(), nullable=False),
        sa.Column("emails_sent", sa.Integer(), nullable=False),
    )
    op.create_index("ix_job_runs_aoi_id", "job_runs", ["aoi_id"])
    op.create_index("ix_job_runs_started_utc", "job_runs", ["started_utc"])
    op.add_column("notifications", sa.Column("pass_utc", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("notifications", "pass_utc")
    op.drop_table("job_runs")
