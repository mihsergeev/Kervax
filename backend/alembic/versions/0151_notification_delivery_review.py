"""Record operator review separately from the notification delivery outcome."""
from alembic import op
import sqlalchemy as sa

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_deliveries", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notification_deliveries", sa.Column("reviewed_by", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_deliveries", "reviewed_by")
    op.drop_column("notification_deliveries", "reviewed_at")
