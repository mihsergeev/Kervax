"""Резервный сервер бэкапов (app/bsrv_mirror.py): какие репозитории основного копировать и на какой
резервный, итог последней рассылки настроек и галочка роли "резервный" у сервера."""

import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_mirror", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("backup_mirror_role", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("backup_mirror_sync", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "backup_mirror_sync")
    op.drop_column("servers", "backup_mirror_role")
    op.drop_column("servers", "backup_mirror")
