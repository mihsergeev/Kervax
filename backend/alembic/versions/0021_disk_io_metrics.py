"""server_metrics: дисковый I/O (чтение/запись байт/сек + IOPS)

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("disk_read", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("disk_write", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("disk_iops", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "disk_iops")
    op.drop_column("server_metrics", "disk_write")
    op.drop_column("server_metrics", "disk_read")
