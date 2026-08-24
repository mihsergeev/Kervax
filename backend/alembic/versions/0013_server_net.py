"""server_metrics: disk_percent + net_rx/net_tx

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("disk_percent", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("net_rx", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("net_tx", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "net_tx")
    op.drop_column("server_metrics", "net_rx")
    op.drop_column("server_metrics", "disk_percent")
