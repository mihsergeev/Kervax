"""servers/checks: точечное заглушение типов алертов (alert_mutes)

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("alert_mutes", sa.JSON(), nullable=True))
    op.add_column("checks", sa.Column("alert_mutes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "alert_mutes")
    op.drop_column("servers", "alert_mutes")
