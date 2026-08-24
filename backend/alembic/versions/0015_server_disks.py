"""server_metrics: заполнение по каждому маунту (JSON) для графика дисков

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("disks", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "disks")
