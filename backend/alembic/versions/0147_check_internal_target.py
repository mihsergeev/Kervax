"""Record whether new monitors can connect to internal targets."""
from alembic import op
import sqlalchemy as sa

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("allow_internal_target", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "allow_internal_target")
