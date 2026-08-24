"""servers: alert_snoozes — точечный временный снуз отдельных типов алертов

Revision ID: 0042
Revises: 0041
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("alert_snoozes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "alert_snoozes")
