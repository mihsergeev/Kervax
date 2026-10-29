"""Отметка "починили" у сервера (app/fixed.py): алерт закрывается сейчас, счет по окну - с отметки."""

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("alert_fixed", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "alert_fixed")
