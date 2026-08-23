"""checks: отдельный порог алерта для деградации

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("degraded_after_failures", sa.Integer(), nullable=False, server_default="5"),
    )


def downgrade() -> None:
    op.drop_column("checks", "degraded_after_failures")
