"""Связь между серверами и трафик за месяц с лимитом тарифа."""

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("links_state", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("traffic_ctr", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("traffic_sum", sa.JSON(), nullable=True))
    op.add_column("servers", sa.Column("traffic_limit_gb", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("servers", sa.Column("traffic_count", sa.String(8), nullable=False, server_default="out"))
    op.add_column("servers", sa.Column("traffic_cycle_day", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "server_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("target_server_id", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(64), nullable=False, server_default=""),
        sa.Column("win", sa.JSON(), nullable=True),
        sa.Column("last_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_loss", sa.Float(), nullable=True),
        sa.Column("last_rtt", sa.Float(), nullable=True),
        sa.Column("last_err", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_server_links_server_id", "server_links", ["server_id"])
    op.create_table(
        "link_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("link_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loss", sa.Float(), nullable=False),
        sa.Column("rtt", sa.Float(), nullable=True),
        sa.Column("rtt_max", sa.Float(), nullable=True),
    )
    op.create_index("ix_link_samples_lookup", "link_samples", ["link_id", "ts"])
    op.create_index("ix_link_samples_ts", "link_samples", ["ts"])
    op.create_table(
        "server_traffic",
        sa.Column("server_id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("rx", sa.BigInteger(), nullable=False),
        sa.Column("tx", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("server_traffic")
    op.drop_index("ix_link_samples_ts", table_name="link_samples")
    op.drop_index("ix_link_samples_lookup", table_name="link_samples")
    op.drop_table("link_samples")
    op.drop_index("ix_server_links_server_id", table_name="server_links")
    op.drop_table("server_links")
    for col in ("traffic_cycle_day", "traffic_count", "traffic_limit_gb", "traffic_sum", "traffic_ctr",
                "links_state"):
        op.drop_column("servers", col)
