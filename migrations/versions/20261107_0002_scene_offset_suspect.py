"""Passes whose offset flag contradicts their pixels (docs/spikes.md, section 5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenes",
        sa.Column("offset_suspect", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("scenes", "offset_suspect")
