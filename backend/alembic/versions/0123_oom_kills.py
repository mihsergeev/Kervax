"""Убийства OOM killer из журнала ядра (helper units-setup 0.5): строки, из которых панель
собирает эпизоды нехватки памяти (app/oom.py)."""

import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oom_kills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.UniqueConstraint("server_id", "key", name="uq_oom_kills_key"),
    )
    op.create_index("ix_oom_kills_lookup", "oom_kills", ["server_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_oom_kills_lookup", table_name="oom_kills")
    op.drop_table("oom_kills")
