"""AOIs, the passes over them, and their monthly composites.

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "aois",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("geometry", Geometry("POLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.Column("epsg", sa.Integer(), nullable=False),
        sa.Column("area_ha", sa.Float(), nullable=False),
        sa.Column("archive_start", sa.Date(), nullable=False),
        sa.Column(
            "created_utc",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("progress_done", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.create_index("ix_aois_geometry", "aois", ["geometry"], postgresql_using="gist")
    op.create_table(
        "scenes",
        sa.Column(
            "aoi_id",
            sa.Integer(),
            sa.ForeignKey("aois.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("item_id", sa.String(), primary_key=True),
        sa.Column("catalog", sa.String(32), nullable=False),
        sa.Column("acquired_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("mgrs_tile", sa.String(8), nullable=False),
        sa.Column("processing_baseline", sa.String(8), nullable=False),
        sa.Column("tile_cloud_pct", sa.Float(), nullable=False),
        sa.Column("read_status", sa.String(16), nullable=False),
        sa.Column("aoi_clear_frac", sa.Float(), nullable=True),
        sa.Column("bytes_read", sa.BigInteger(), nullable=False),
        sa.Column("full_bytes", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_scenes_acquired_utc", "scenes", ["acquired_utc"])
    op.create_table(
        "months",
        sa.Column(
            "aoi_id",
            sa.Integer(),
            sa.ForeignKey("aois.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("passes", sa.Integer(), nullable=False),
        sa.Column("with_bands", sa.Integer(), nullable=False),
        sa.Column("clear_scenes", sa.Integer(), nullable=False),
        sa.Column("observed_frac", sa.Float(), nullable=False),
        sa.Column("filled_frac", sa.Float(), nullable=False),
        sa.Column("ndvi_mean", sa.Float(), nullable=True),
        sa.Column("version", sa.String(16), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("months")
    op.drop_table("scenes")
    op.drop_table("aois")
