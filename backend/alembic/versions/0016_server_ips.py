"""servers: локальный и внешний IP (для карточки сервера вместо ОС)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("local_ip", sa.String(64), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("external_ip", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("servers", "external_ip")
    op.drop_column("servers", "local_ip")
