"""Вход админа под чужой учеткой (app/impersonate.py): сеансы такого входа и пометка в журнале
действий, от чьего имени действовал админ."""

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "impersonations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admin", sa.String(64), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column("audit_log", sa.Column("as_user", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("audit_log", "as_user")
    op.drop_table("impersonations")
