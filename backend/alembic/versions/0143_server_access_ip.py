"""Store an operator-configured server access address separately from agent sources."""
import sqlalchemy as sa
from alembic import op

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("access_ip", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "access_ip")
