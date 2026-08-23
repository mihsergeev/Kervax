"""servers + server_metrics (агент-push)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("group_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("hostname", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("os", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("agent_version", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("last_report", sa.JSON(), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cpu_alert_percent", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("mem_alert_percent", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("disk_alert_percent", sa.Integer(), nullable=False, server_default="90"),
        sa.Column("offline_after_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("alert_state", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_servers_token_hash", "servers", ["token_hash"])
    op.create_table(
        "server_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("mem_percent", sa.Float(), nullable=True),
        sa.Column("load1", sa.Float(), nullable=True),
    )
    op.create_index("ix_server_metrics_server_id", "server_metrics", ["server_id"])
    op.create_index("ix_server_metrics_ts", "server_metrics", ["ts"])
    op.create_index("ix_server_metrics_lookup", "server_metrics", ["server_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_server_metrics_lookup", table_name="server_metrics")
    op.drop_index("ix_server_metrics_ts", table_name="server_metrics")
    op.drop_index("ix_server_metrics_server_id", table_name="server_metrics")
    op.drop_table("server_metrics")
    op.drop_index("ix_servers_token_hash", table_name="servers")
    op.drop_table("servers")
