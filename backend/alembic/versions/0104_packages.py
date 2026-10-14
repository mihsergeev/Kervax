"""Пакеты на серверах: список, отпечаток у сервера, подробности событий хронологии."""

import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_packages",
        sa.Column("server_id", sa.Integer(), primary_key=True),
        sa.Column("mgr", sa.String(8), nullable=False, server_default=""),
        sa.Column("hash", sa.String(32), nullable=False, server_default=""),
        sa.Column("n", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending", sa.JSON(), nullable=True),
        sa.Column("ev_ts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ev_keys", sa.JSON(), nullable=True),
    )
    op.add_column("servers", sa.Column("pkg_hash", sa.String(32), nullable=False, server_default=""))
    op.add_column("server_events", sa.Column("data", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_events", "data")
    op.drop_column("servers", "pkg_hash")
    op.drop_table("server_packages")
