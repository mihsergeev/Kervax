"""checks: ignore_tls — не проверять TLS-сертификат основной проверкой

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "checks",
        sa.Column("ignore_tls", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("checks", "ignore_tls")
