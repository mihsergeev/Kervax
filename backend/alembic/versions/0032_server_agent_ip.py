"""servers: agent_ip — адрес, с которого агент ходит в панель (для фаервола)

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("agent_ip", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("servers", "agent_ip")
