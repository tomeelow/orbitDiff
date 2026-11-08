"""How much of an event's footprint changed on the passes that decided it.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("confidence", sa.Float()))


def downgrade() -> None:
    op.drop_column("events", "confidence")
