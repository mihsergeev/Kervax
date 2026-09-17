"""Additional paths of a monitor: the site and its API on one domain in one monitor.

A monitor checked one address. A second monitor for /health on the same domain doubled the
certificate and domain checks together with their alerts. Now the monitor keeps extra paths
(checks.extra_paths) and the split of its last check by them (checks.last_path_results). For a
local monitor the agent answers every path as a separate task, results are kept in
agent_path_probes.
"""

from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("extra_paths", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("checks", sa.Column("paths_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("checks", sa.Column("last_path_results", sa.JSON(), nullable=True))
    op.create_table(
        "agent_path_probes",
        sa.Column("check_id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.String(256), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(512), nullable=False, server_default=""),
        sa.Column("via", sa.String(128), nullable=False, server_default=""),
        sa.Column("manual_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_path_probes_server_id", "agent_path_probes", ["server_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_path_probes_server_id", table_name="agent_path_probes")
    op.drop_table("agent_path_probes")
    op.drop_column("checks", "last_path_results")
    op.drop_column("checks", "paths_changed_at")
    op.drop_column("checks", "extra_paths")
