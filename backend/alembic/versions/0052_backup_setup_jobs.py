"""backup_setup_jobs (фоновая оркестрация «настроить бэкап»)

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_setup_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False, index=True),
        sa.Column("backup_server_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("steps", sa.JSON(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True),
    )


def downgrade() -> None:
    op.drop_table("backup_setup_jobs")
