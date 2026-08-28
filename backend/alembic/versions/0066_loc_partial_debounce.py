"""Дебаунс алерта о частичной доступности.

После полного падения точки проверки поднимаются не одновременно: сайт уже
отвечает из одной, ещё не отвечает из другой. Панель принимала эту фазу за
настоящую частичную недоступность и на каждое восстановление слала лишнюю пару
«недоступен из N» + «снова доступен отовсюду». `loc_partial_since` даёт выдержку
и обнуляется при полном падении, `loc_notified` отделяет «о чём уведомили» от
«что показываем», чтобы отбой не уходил по алерту, которого не было.
"""

from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("loc_partial_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("checks", sa.Column("loc_notified", sa.JSON(), nullable=True))
    # Кто уже был в состоянии частичной недоступности — считается уведомлённым:
    # иначе после обновления по ним придёт отбой без предшествующего алерта.
    op.execute("update checks set loc_notified = loc_alerted where loc_alerted is not null")


def downgrade() -> None:
    op.drop_column("checks", "loc_notified")
    op.drop_column("checks", "loc_partial_since")
