"""Хронология сервера: события (контейнеры, перезагрузки, пакеты, версии, порты) и снимок для сравнения."""

import sqlalchemy as sa
from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cat", sa.String(12), nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False, server_default=""),
        sa.Column("obj", sa.String(255), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("n", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_server_events_server_ts", "server_events", ["server_id", "ts"])
    op.create_index("ix_server_events_ts", "server_events", ["ts"])
    op.create_table(
        "timeline_state",
        sa.Column("server_id", sa.Integer(), primary_key=True),
        sa.Column("state", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("timeline_state")
    op.drop_index("ix_server_events_ts", "server_events")
    op.drop_index("ix_server_events_server_ts", "server_events")
    op.drop_table("server_events")
