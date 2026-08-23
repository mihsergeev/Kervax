"""checks.location_ids — выбор локаций на монитор

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL = все включённые локации (сохраняет прежнее поведение существующих мониторов)
    op.add_column("checks", sa.Column("location_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "location_ids")
