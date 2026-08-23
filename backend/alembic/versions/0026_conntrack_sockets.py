"""server_metrics: conntrack (count/max) + сокеты (used/tcp/tw/udp)

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_COLS = ("conntrack_count", "conntrack_max", "sock_used", "sock_tcp", "sock_tcp_tw", "sock_udp")


def upgrade() -> None:
    for c in _COLS:
        op.add_column("server_metrics", sa.Column(c, sa.Float(), nullable=True))


def downgrade() -> None:
    for c in reversed(_COLS):
        op.drop_column("server_metrics", c)
