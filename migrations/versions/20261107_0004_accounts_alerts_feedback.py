"""Accounts and AOI owners, alert rules, the emails sent and the feedback given (Phase 9).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _utc(name: str, **kwargs) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        _utc("created_utc", server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "login_links",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        _utc("expires_utc", nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        _utc("expires_utc", nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # Existing AOIs have no owner until `orbitdiff assign-aois` gives them one.
    op.add_column(
        "aois",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
    )
    op.create_index("ix_aois_user_id", "aois", ["user_id"])

    op.create_table(
        "alert_rules",
        sa.Column(
            "aoi_id",
            sa.Integer(),
            sa.ForeignKey("aois.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("skip_seasonal", sa.Boolean(), nullable=False),
        sa.Column("min_ha", sa.Float(), nullable=False),
        _utc("watching_since"),
        _utc("updated_utc", server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("event_status", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.String()),
        _utc("claimed_utc", nullable=False),
        _utc("sent_utc"),
        sa.UniqueConstraint("event_id", "user_id", "kind"),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("via", sa.String(8), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        _utc("created_utc", server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("event_id", "user_id"),
    )


def downgrade() -> None:
    for table in ("feedback", "notifications", "alert_rules"):
        op.drop_table(table)
    op.drop_index("ix_aois_user_id", "aois")
    op.drop_column("aois", "user_id")
    for table in ("sessions", "login_links", "users"):
        op.drop_table(table)
