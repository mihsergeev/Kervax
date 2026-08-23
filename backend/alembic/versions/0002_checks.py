"""checks and check_samples

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=512), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("group_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("degraded_ms", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("method", sa.String(length=8), nullable=False, server_default="GET"),
        sa.Column("expected_status", sa.String(length=32), nullable=False, server_default="200-399"),
        sa.Column("keyword_up", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("keyword_down", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("cert_warn_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("last_status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("last_message", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("last_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_table(
        "check_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("message", sa.String(length=512), nullable=False, server_default=""),
    )
    op.create_index("ix_check_samples_check_id", "check_samples", ["check_id"])
    op.create_index("ix_check_samples_ts", "check_samples", ["ts"])
    op.create_index("ix_check_samples_lookup", "check_samples", ["check_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_check_samples_lookup", table_name="check_samples")
    op.drop_index("ix_check_samples_ts", table_name="check_samples")
    op.drop_index("ix_check_samples_check_id", table_name="check_samples")
    op.drop_table("check_samples")
    op.drop_table("checks")
