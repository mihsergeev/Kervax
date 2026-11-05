"""Add opt-in Qdrant health monitoring."""
from alembic import op
import sqlalchemy as sa

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("qdrant_monitor_enabled", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("servers", "qdrant_monitor_enabled")
