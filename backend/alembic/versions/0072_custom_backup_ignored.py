"""Свои бэкапы ноды: какие из найденных заданий не отслеживать.

Панель находит на ноде бэкапы, настроенные без неё (cron, systemd-таймеры, скрипты с
метриками), и следит за их работой. Поиск эвристический: «это не бэкап» или «этот старый
скрипт больше не нужен» снимается отметкой, которая живёт здесь.
"""

from alembic import op
import sqlalchemy as sa

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("custom_backup_ignored", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "custom_backup_ignored")
