"""Левые запросы к панели: по минутам и адресам, только отказы и всплески (app/request_watch.py)."""

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_stats",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bad", sa.Integer, nullable=False, server_default="0"),
        sa.Column("codes", sa.JSON, nullable=False),
        sa.Column("paths", sa.JSON, nullable=False),
    )
    op.create_index("ix_request_stats_minute", "request_stats", ["minute"])
    op.create_index("ix_request_stats_key", "request_stats", ["key"])


def downgrade() -> None:
    op.drop_index("ix_request_stats_key", "request_stats")
    op.drop_index("ix_request_stats_minute", "request_stats")
    op.drop_table("request_stats")
