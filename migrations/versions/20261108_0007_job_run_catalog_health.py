"""Catalog errors, fallback searches and read retries per run (Phase 10).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

COLUMNS = ("read_retries", "search_errors", "search_fallbacks")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("job_runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("job_runs", name, server_default=None)


def downgrade() -> None:
    for name in COLUMNS:
        op.drop_column("job_runs", name)
