"""Персональные Telegram-алерты: свой чат у каждой учётки.

Раньше канал был один на панель: все алерты валились в общий чат, и «кому это
чинить» приходилось определять глазами. Теперь сотрудник привязывает свой чат и
получает только то, что видит в панели (его группы и разделы).

tg_token — необязательный: пусто = шлём общим ботом панели. Он нужен, только если
человеку нужен отдельный бот или свой прокси к api.telegram.org.
"""

from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("tg_chat_id", sa.String(length=64),
                                     nullable=False, server_default=""))
    op.add_column("users", sa.Column("tg_token", sa.String(length=256),
                                     nullable=False, server_default=""))
    op.add_column("users", sa.Column("tg_alerts", sa.Boolean(),
                                     nullable=False, server_default=sa.true()))
    # одноразовый код привязки: человек шлёт его боту, панель находит в getUpdates
    op.add_column("users", sa.Column("tg_link_code", sa.String(length=32),
                                     nullable=False, server_default=""))


def downgrade() -> None:
    for col in ("tg_link_code", "tg_alerts", "tg_token", "tg_chat_id"):
        op.drop_column("users", col)
