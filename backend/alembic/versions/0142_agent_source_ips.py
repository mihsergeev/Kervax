"""Explicit extra source IPs for agent API requests."""
import sqlalchemy as sa
from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Старые agent_ip/external_ip/отчеты не становятся дополнительными источниками.
    op.add_column("servers", sa.Column("agent_source_ips", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("servers", "agent_source_ips")
