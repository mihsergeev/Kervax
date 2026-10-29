"""Пометка "сервер разработки" (app/dev_mode.py): поведение приложений не будит алертами."""

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("is_dev", sa.Boolean(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("servers", "is_dev")
