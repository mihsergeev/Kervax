"""Store operator-selected Windows service names."""
import sqlalchemy as sa
from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("windows_service_names", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "windows_service_names")
