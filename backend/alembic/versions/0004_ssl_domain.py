"""checks: SSL/domain expiry monitoring fields

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


_COLUMNS = [
    ("check_ssl", sa.Column("check_ssl", sa.Boolean(), nullable=False, server_default=sa.true())),
    ("check_domain", sa.Column("check_domain", sa.Boolean(), nullable=False, server_default=sa.true())),
    ("domain_warn_days", sa.Column("domain_warn_days", sa.Integer(), nullable=False, server_default="30")),
    ("ssl_days", sa.Column("ssl_days", sa.Integer(), nullable=True)),
    ("domain_days", sa.Column("domain_days", sa.Integer(), nullable=True)),
    ("ssl_message", sa.Column("ssl_message", sa.String(length=256), nullable=False, server_default="")),
    ("domain_message", sa.Column("domain_message", sa.String(length=256), nullable=False, server_default="")),
    ("expiry_checked_at", sa.Column("expiry_checked_at", sa.DateTime(timezone=True), nullable=True)),
    ("ssl_notified", sa.Column("ssl_notified", sa.Boolean(), nullable=False, server_default=sa.false())),
    ("domain_notified", sa.Column("domain_notified", sa.Boolean(), nullable=False, server_default=sa.false())),
]


def upgrade() -> None:
    for _, col in _COLUMNS:
        op.add_column("checks", col)


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("checks", name)
