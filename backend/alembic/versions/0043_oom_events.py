"""oom_events — журнал OOM-киллов (когда + кого убило ядро)

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oom_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("victim", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_oom_events_lookup", "oom_events", ["server_id", "ts"])
    op.create_index("ix_oom_events_ts", "oom_events", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_oom_events_ts", table_name="oom_events")
    op.drop_index("ix_oom_events_lookup", table_name="oom_events")
    op.drop_table("oom_events")
