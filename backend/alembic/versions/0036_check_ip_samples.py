"""check_ip_samples — тайм-серия времени ответа по каждому IP

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "check_ip_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_check_ip_samples_lookup", "check_ip_samples", ["check_id", "ip", "ts"])
    op.create_index("ix_check_ip_samples_ts", "check_ip_samples", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_check_ip_samples_ts", table_name="check_ip_samples")
    op.drop_index("ix_check_ip_samples_lookup", table_name="check_ip_samples")
    op.drop_table("check_ip_samples")
