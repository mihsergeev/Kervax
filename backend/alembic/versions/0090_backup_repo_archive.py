"""Архив бэкап-сервера.

servers.backup_repo_archive - репозитории и старые группы снапшотов, которые хранятся как есть:
сервера больше нет, бэкапить больше не нужно, разовый бэкап, проект заморожен. Не устаревают и
не считаются проблемой, проверка целостности по ним идет.
"""

from alembic import op
import sqlalchemy as sa

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_repo_archive", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "backup_repo_archive")
