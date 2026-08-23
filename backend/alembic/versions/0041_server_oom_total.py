"""servers: oom_total (кумулятивно) + oom_victim — надёжный OOM-алерт

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("oom_total", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("oom_victim", sa.String(length=200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("servers", "oom_victim")
    op.drop_column("servers", "oom_total")
