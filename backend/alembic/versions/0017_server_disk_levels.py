"""servers: уровни диска (warn/crit) + момент перезагрузки

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("disk_warn_percent", sa.Integer(), nullable=False, server_default="85"),
    )
    op.add_column(
        "servers",
        sa.Column("disk_crit_percent", sa.Integer(), nullable=False, server_default="95"),
    )
    op.add_column(
        "servers", sa.Column("rebooted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("servers", "rebooted_at")
    op.drop_column("servers", "disk_crit_percent")
    op.drop_column("servers", "disk_warn_percent")
