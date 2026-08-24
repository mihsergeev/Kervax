"""checks: last_ip_results — разбивка последней проверки по IP

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("last_ip_results", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "last_ip_results")
