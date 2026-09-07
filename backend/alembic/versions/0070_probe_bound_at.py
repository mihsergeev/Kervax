"""Момент привязки локального монитора к ноде.

Свежесозданный локальный монитор минуту показывал «проверять локально некому» и
успевал открыть инцидент: привязка считалась уже ПОСЛЕ проверок, а агент забирает
задание своим следующим отчётом. Отметка времени привязки даёт планировщику
понять, что данных ещё нет не потому, что сайт лежит.
"""

from alembic import op
import sqlalchemy as sa

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("probe_bound_at", sa.DateTime(timezone=True), nullable=True),
    )
    # у существующих мониторов результат давно ходит — прогрев им не нужен
    op.execute("update checks set probe_bound_at = created_at where probe_local")


def downgrade() -> None:
    op.drop_column("checks", "probe_bound_at")
