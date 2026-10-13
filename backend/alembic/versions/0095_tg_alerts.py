"""Кнопки под алертами в Telegram: запись об отправленном алерте, на которую ссылается кнопка."""

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tg_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False, server_default=""),
        sa.Column("kind", sa.String(40), nullable=False, server_default=""),
        sa.Column("target", sa.String(255), nullable=False, server_default=""),
        sa.Column("section", sa.String(20), nullable=False, server_default=""),
        sa.Column("grp", sa.String(255), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("messages", sa.JSON(), nullable=True),
        sa.Column("acked_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted", sa.String(255), nullable=False, server_default=""),
        sa.Column("undo", sa.JSON(), nullable=True),
        sa.Column("esc_level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("esc_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tg_alerts_ts", "tg_alerts", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_tg_alerts_ts", table_name="tg_alerts")
    op.drop_table("tg_alerts")
