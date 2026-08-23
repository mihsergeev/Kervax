"""check_incidents + checks.consecutive_fails

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("consecutive_fails", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "check_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_check_incidents_check_id", "check_incidents", ["check_id"])
    op.create_index(
        "ix_check_incidents_lookup", "check_incidents", ["check_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_check_incidents_lookup", table_name="check_incidents")
    op.drop_index("ix_check_incidents_check_id", table_name="check_incidents")
    op.drop_table("check_incidents")
    op.drop_column("checks", "consecutive_fails")
