"""Отложенные сроки сайта: checks.expiry_snooze.

"Напомнить за 7 дней", "напомнить за 1 день" и "не следить" у срока домена и сертификата:
пункт уходит с главной и из алертов до выбранного порога или навсегда. Продление снимает
отложку само, "не следить" остается.
"""

import sqlalchemy as sa
from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("expiry_snooze", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "expiry_snooze")
