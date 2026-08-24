"""checks: check_all_ips — проверять все A-адреса домена

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("check_all_ips", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("checks", "check_all_ips")
