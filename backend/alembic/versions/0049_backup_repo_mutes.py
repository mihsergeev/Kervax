"""backup_repo_mutes — заглушённые репозитории бэкап-сервера

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_repo_mutes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "backup_repo_mutes")
