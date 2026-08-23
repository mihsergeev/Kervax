"""ночное окно бэкапа: дедлайн завершения + флаг «любое время»

Revision ID: 0055
Revises: 0054
"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_deadline_hour", sa.Integer(),
                                       nullable=False, server_default="8"))
    op.add_column("servers", sa.Column("backup_anytime", sa.Boolean(),
                                       nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("servers", "backup_anytime")
    op.drop_column("servers", "backup_deadline_hour")
