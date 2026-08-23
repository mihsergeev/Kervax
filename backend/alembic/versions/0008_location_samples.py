"""location_samples — тайм-серия проверок по локациям

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "location_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_location_samples_check_id", "location_samples", ["check_id"])
    op.create_index(
        "ix_location_samples_location_id", "location_samples", ["location_id"]
    )
    op.create_index("ix_location_samples_ts", "location_samples", ["ts"])
    op.create_index(
        "ix_location_samples_lookup",
        "location_samples",
        ["check_id", "location_id", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_location_samples_lookup", table_name="location_samples")
    op.drop_index("ix_location_samples_ts", table_name="location_samples")
    op.drop_index("ix_location_samples_location_id", table_name="location_samples")
    op.drop_index("ix_location_samples_check_id", table_name="location_samples")
    op.drop_table("location_samples")
