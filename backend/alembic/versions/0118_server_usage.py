"""Кто качает трафик и кто пишет на диск по часам (app/usage.py): за сутки и за неделю, а не
только за последний час из отчета агента."""

import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_usage",
        sa.Column("server_id", sa.Integer(), primary_key=True),
        sa.Column("metric", sa.String(8), primary_key=True),
        sa.Column("hour", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("a", sa.BigInteger(), nullable=False),
        sa.Column("b", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_server_usage_hour", "server_usage", ["hour"])
    op.add_column("servers", sa.Column("usage_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "usage_at")
    op.drop_index("ix_server_usage_hour", table_name="server_usage")
    op.drop_table("server_usage")
