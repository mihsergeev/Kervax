"""servers: целевая версия агента для управляемого обновления

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("target_agent_version", sa.String(length=32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("servers", "target_agent_version")
