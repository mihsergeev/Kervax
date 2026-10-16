"""Куда локация выходит в интернет: адрес и страна по последней проверке прокси."""

import sqlalchemy as sa
from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("locations", sa.Column("exit_ip", sa.String(64), nullable=False, server_default=""))
    op.add_column("locations", sa.Column("exit_country", sa.String(2), nullable=False, server_default=""))
    op.add_column("locations", sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("locations", "exit_at")
    op.drop_column("locations", "exit_country")
    op.drop_column("locations", "exit_ip")
