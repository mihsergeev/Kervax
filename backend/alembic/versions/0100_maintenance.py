"""Окна обслуживания: алерты по выбранным серверам и мониторам молчат, после окна сверка."""

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_windows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("server_ids", sa.JSON(), nullable=True),
        sa.Column("check_ids", sa.JSON(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repeat", sa.String(10), nullable=False, server_default=""),
        sa.Column("weekdays", sa.JSON(), nullable=True),
        sa.Column("verify", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("verified_end", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("maintenance_windows")
