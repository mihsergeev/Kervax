"""Галочка «проверять локально» вместо выбора сервера.

Человек решает ЧТО (проверять изнутри), а не КЕМ: панель и так знает, чьи
веб-серверы обслуживают домен — агенты присылают их в web_services. Выбор
конкретной ноды был лишней работой и лишним поводом ошибиться, а при переезде
сайта ещё и устаревал молча.
"""

from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("probe_local", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # кто уже был назначен вручную — тот и остаётся локальным
    op.execute("update checks set probe_local = true where probe_server_id is not null")


def downgrade() -> None:
    op.drop_column("checks", "probe_local")
