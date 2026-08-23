"""backup_commands.payload (доп. параметры провижининга, чистятся при завершении)

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_commands", sa.Column("payload", sa.JSON(), nullable=True))
    # расширяем action под новые провижининг-действия
    op.alter_column(
        "backup_commands", "action",
        existing_type=sa.String(length=20), type_=sa.String(length=24),
    )


def downgrade() -> None:
    op.drop_column("backup_commands", "payload")
    op.alter_column(
        "backup_commands", "action",
        existing_type=sa.String(length=24), type_=sa.String(length=20),
    )
