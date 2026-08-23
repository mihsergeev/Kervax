"""backup_commands — очередь backup-действий (set_paths/set_schedule/run_now)

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False, server_default="exclude"),
        sa.Column("paths", sa.JSON(), nullable=True),
        sa.Column("schedule", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_backup_commands_lookup", "backup_commands", ["server_id", "status"])
    op.create_index("ix_backup_commands_server_id", "backup_commands", ["server_id"])
    op.create_index("ix_backup_commands_created_at", "backup_commands", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backup_commands_created_at", table_name="backup_commands")
    op.drop_index("ix_backup_commands_server_id", table_name="backup_commands")
    op.drop_index("ix_backup_commands_lookup", table_name="backup_commands")
    op.drop_table("backup_commands")
