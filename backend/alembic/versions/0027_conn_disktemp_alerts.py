"""servers: пороги алертов conntrack (заполнение %) и температуры диска (°C)

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("conntrack_alert_percent", sa.Integer(), nullable=False, server_default="90"),
    )
    op.add_column(
        "servers",
        sa.Column("disk_temp_alert_c", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("servers", "disk_temp_alert_c")
    op.drop_column("servers", "conntrack_alert_percent")
