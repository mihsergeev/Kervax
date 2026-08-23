"""checks: HTTP Basic auth (auth_method/auth_user/auth_pass)

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("auth_method", sa.String(length=16), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("auth_user", sa.String(length=128), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("auth_pass", sa.String(length=256), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("checks", "auth_pass")
    op.drop_column("checks", "auth_user")
    op.drop_column("checks", "auth_method")
