"""Ретранслятор для агентов (app/relay.py): прокси на одном из серверов, через который в панель
ходят агенты, чей адрес режут на пути к ней, и выбор ретранслятора у сервера-клиента."""

import sqlalchemy as sa
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("relay_enabled", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("relay_port", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("relay_user", sa.String(64), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("relay_password", sa.String(128), nullable=False, server_default=""))
    op.add_column("servers", sa.Column("relay_state", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("relay_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("relay_via_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ("relay_via_id", "relay_seen_at", "relay_state", "relay_password", "relay_user",
                "relay_port", "relay_enabled"):
        op.drop_column("servers", col)
