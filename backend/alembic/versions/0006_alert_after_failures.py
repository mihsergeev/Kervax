"""checks.alert_after_failures — порог алерта на монитор

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column(
            "alert_after_failures", sa.Integer(), nullable=False, server_default="2"
        ),
    )


def downgrade() -> None:
    op.drop_column("checks", "alert_after_failures")
