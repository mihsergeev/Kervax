"""SSL/домен: списки порогов + эскалационные флаги

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # cert_warn_days(int) → ssl_warn_days(JSON [14,7,1])
    op.drop_column("checks", "cert_warn_days")
    op.add_column(
        "checks",
        sa.Column("ssl_warn_days", sa.JSON(), nullable=False,
                  server_default=sa.text("'[14, 7, 1]'")),
    )
    # domain_warn_days(int) → JSON [7,1]
    op.drop_column("checks", "domain_warn_days")
    op.add_column(
        "checks",
        sa.Column("domain_warn_days", sa.JSON(), nullable=False,
                  server_default=sa.text("'[7, 1]'")),
    )
    # дедуп-флаги bool → «какой порог уже оповещён» (int, nullable)
    op.drop_column("checks", "ssl_notified")
    op.drop_column("checks", "domain_notified")
    op.add_column("checks", sa.Column("ssl_alerted_days", sa.Integer(), nullable=True))
    op.add_column("checks", sa.Column("domain_alerted_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("checks", "domain_alerted_days")
    op.drop_column("checks", "ssl_alerted_days")
    op.add_column(
        "checks",
        sa.Column("domain_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "checks",
        sa.Column("ssl_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_column("checks", "domain_warn_days")
    op.add_column(
        "checks",
        sa.Column("domain_warn_days", sa.Integer(), nullable=False, server_default="30"),
    )
    op.drop_column("checks", "ssl_warn_days")
    op.add_column(
        "checks",
        sa.Column("cert_warn_days", sa.Integer(), nullable=False, server_default="14"),
    )
