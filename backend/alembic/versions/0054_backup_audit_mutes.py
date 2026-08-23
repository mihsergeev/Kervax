"""приглушение отдельных находок аудита покрытия

Revision ID: 0054
Revises: 0053
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("backup_audit_mutes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "backup_audit_mutes")
