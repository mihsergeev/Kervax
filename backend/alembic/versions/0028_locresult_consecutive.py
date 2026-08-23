"""location_results: счётчик подряд-фейлов для дебаунса локационных алертов

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "location_results",
        sa.Column("consecutive_fails", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("location_results", "consecutive_fails")
