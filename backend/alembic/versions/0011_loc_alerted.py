"""checks.loc_alerted — дедуп алертов частичной доступности

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("loc_alerted", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "loc_alerted")
