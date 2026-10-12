"""Локация только для проверки портов.

locations.monitors - гонять ли через локацию мониторы сайтов. Выключено - прокси нужен только
для внешней проверки портов: сайты, открытые лишь для сети панели, не дают ложных алертов
"недоступен из ...".
"""

from alembic import op
import sqlalchemy as sa

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("locations", sa.Column("monitors", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("locations", "monitors")
