"""checks + servers: snooze_until — быстрое приглушение алертов на время

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("servers", sa.Column("snooze_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "snooze_until")
    op.drop_column("checks", "snooze_until")
