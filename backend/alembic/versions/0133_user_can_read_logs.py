"""Галка учетки только для просмотра: users.can_read_logs.

Логи контейнеров, подов и журнал systemd учетке только для просмотра закрыты: в логах бывают
пароли и токены. Админ может разрешить ей чтение логов галкой в окне "Пользователи". По
умолчанию выключено, у существующих учеток тоже.
"""

import sqlalchemy as sa
from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("can_read_logs", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("users", "can_read_logs")
