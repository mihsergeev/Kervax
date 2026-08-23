"""server_metrics: разбивка сети по интерфейсам и диска по устройствам (util/await)

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("net_ifaces", sa.JSON(), nullable=True))
    op.add_column("server_metrics", sa.Column("disk_devs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "disk_devs")
    op.drop_column("server_metrics", "net_ifaces")
