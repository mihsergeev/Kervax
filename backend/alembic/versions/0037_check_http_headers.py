"""checks: http_headers — кастомные HTTP-заголовки (JSON-текст)

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("http_headers", sa.String(length=4096), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("checks", "http_headers")
