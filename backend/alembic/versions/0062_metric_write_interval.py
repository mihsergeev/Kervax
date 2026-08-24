"""Реже писать метрики в историю: отметка последней записи у сервера.

Агент шлёт отчёт часто (это нужно для «онлайн» и алертов), и панель писала
строку в server_metrics на КАЖДЫЙ отчёт — по 55 тысяч строк в сутки с десяти
нод, самая тяжёлая таблица в базе (1,6 ГБ). Для графиков такая частота избыточна:
достаточно точки раз в минуту. Отметка нужна, чтобы решать это без запроса
max(ts) на каждый входящий отчёт.
"""

from alembic import op
import sqlalchemy as sa

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("metric_written_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "metric_written_at")
