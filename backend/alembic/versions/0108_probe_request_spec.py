"""Настройки пробной проверки нового монитора в задании агенту."""

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("probe_requests", sa.Column("spec", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("probe_requests", "spec")
