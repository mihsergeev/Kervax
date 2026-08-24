"""checks: ручной порядок в списке (sort_order)

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    # начальный порядок = по id (сохраняет текущую раскладку)
    op.execute("UPDATE checks SET sort_order = id")


def downgrade() -> None:
    op.drop_column("checks", "sort_order")
