"""server_metrics: IOPS раздельно на чтение/запись (вместо суммарного)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("disk_read_iops", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("disk_write_iops", sa.Float(), nullable=True))
    op.drop_column("server_metrics", "disk_iops")


def downgrade() -> None:
    op.add_column("server_metrics", sa.Column("disk_iops", sa.Float(), nullable=True))
    op.drop_column("server_metrics", "disk_write_iops")
    op.drop_column("server_metrics", "disk_read_iops")
