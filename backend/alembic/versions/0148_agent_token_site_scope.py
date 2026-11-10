"""Scope credential-bearing site tasks to the agent token issuer's sites."""
from alembic import op
import sqlalchemy as sa

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("token_site_groups", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "token_site_groups")
