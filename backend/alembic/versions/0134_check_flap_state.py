"""Эпизод мигающего сайта: checks.flap_state (app/site_flap.py).

Мигающий сайт давал по сообщению на каждый поворот: "недоступен", "снова отвечает", "через
раз", снова "недоступен". Здесь хранится, что о сайте уже сказали и отбоя еще не было
({"lvl", "at"}), или когда закрыли прошлый эпизод ({"closed"}). Пусто - эпизодов не было.
"""

import sqlalchemy as sa
from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("flap_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "flap_state")
