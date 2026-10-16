"""Защита входа: блокировка адреса после неудачных входов и логин у каждой неудачи.

Раньше 10 неудач за 5 минут давали паузу, пока старые попытки не выйдут из окна, то есть
минут на пять, и подбор шел дальше. Теперь после порога адрес блокируется на заданный срок
(login_bans), повторно за сутки - на сутки. Логин нужен, чтобы в окне и в алерте было видно,
какие учетки пробовали.
"""

import sqlalchemy as sa
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("login_failures", sa.Column("username", sa.String(64), nullable=False, server_default=""))
    op.create_table(
        "login_bans",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("logins", sa.String(255), nullable=False, server_default=""),
        sa.Column("repeat", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("blocked", sa.Integer, nullable=False, server_default="0"),
        sa.Column("alerted", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_login_bans_key", "login_bans", ["key"])
    op.create_index("ix_login_bans_until", "login_bans", ["until"])


def downgrade() -> None:
    op.drop_index("ix_login_bans_until", "login_bans")
    op.drop_index("ix_login_bans_key", "login_bans")
    op.drop_table("login_bans")
    op.drop_column("login_failures", "username")
