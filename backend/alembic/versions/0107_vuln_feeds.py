"""Базы важных уязвимостей по релизам Ubuntu."""

import sqlalchemy as sa
from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vuln_feeds",
        sa.Column("release", sa.String(16), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("gen", sa.String(40), nullable=False, server_default=""),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tried", sa.DateTime(timezone=True), nullable=True),
        sa.Column("err", sa.String(300), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("vuln_feeds")
