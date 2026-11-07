"""Change events, with their statuses and provenance (Phase 7).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07
"""

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "aoi_id", sa.Integer(), sa.ForeignKey("aois.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("reference_month", sa.Date(), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("area_ha", sa.Float(), nullable=False),
        sa.Column(
            "geometry",
            geoalchemy2.Geometry("GEOMETRY", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("ndvi_before", sa.Float()),
        sa.Column("ndvi_after", sa.Float()),
        sa.Column("detected_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("method_version", sa.String(48), nullable=False),
        sa.Column("thresholds", sa.JSON(), nullable=False),
        sa.Column(
            "updated_utc", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_events_aoi_id", "events", ["aoi_id"])
    op.create_index("ix_events_geometry", "events", ["geometry"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_table("events")
