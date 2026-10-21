"""Признаки взлома сайта (app/tamper.py): checks.tamper_state и checks.tamper_sent.

tamper_state - база главной (title, размер, внешние домены), последний разбор, признаки и
отложенное; tamper_sent - о чем уже предупредили в Telegram.
"""

import sqlalchemy as sa
from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("tamper_state", sa.JSON(), nullable=True))
    op.add_column("checks", sa.Column("tamper_sent", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "tamper_sent")
    op.drop_column("checks", "tamper_state")
