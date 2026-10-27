"""Баны сканеров (helper autoban-setup): история банов по серверам для карточки и сводки (app/autoban.py)."""

import sqlalchemy as sa
from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autoban_bans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("paths", sa.JSON(), nullable=False),
        sa.Column("more", sa.Integer(), nullable=False),
        sa.Column("where", sa.String(120), nullable=False),
        sa.Column("watch", sa.Boolean(), nullable=False),
        sa.Column("seen", sa.Integer(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end", sa.String(16), nullable=False),
        sa.Column("digested", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("server_id", "ip", "since", name="uq_autoban_bans_key"),
    )
    op.create_index("ix_autoban_bans_lookup", "autoban_bans", ["server_id", "since"])


def downgrade() -> None:
    op.drop_index("ix_autoban_bans_lookup", table_name="autoban_bans")
    op.drop_table("autoban_bans")
