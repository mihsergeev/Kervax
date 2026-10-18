"""Почему nginx отвечал 502/504 (helper webserver-setup 0.26): до какого upstream он не
достучался и как - в минуты с ошибками 5xx (app/web_upstream.py)."""

import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_error_samples", sa.Column("ups", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_error_samples", "ups")
