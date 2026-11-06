"""Store the acknowledged backup growth run per server."""
from alembic import op
import sqlalchemy as sa

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_growth_ack_ts", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "backup_growth_ack_ts")
