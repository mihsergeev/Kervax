"""Версии без поддержки (app/software.py): пакеты сторонних репозиториев сервера - PHP из sury,
PostgreSQL из pgdg и т.п., - чтобы не читать весь список пакетов на каждый запрос."""

import sqlalchemy as sa
from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("sw_host", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "sw_host")
