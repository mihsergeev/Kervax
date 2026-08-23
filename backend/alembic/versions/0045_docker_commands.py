"""docker_commands — очередь docker-действий (restart/stop/start/logs)

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-17

"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docker_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("container", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("tail", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_docker_commands_lookup", "docker_commands", ["server_id", "status"])
    op.create_index("ix_docker_commands_server_id", "docker_commands", ["server_id"])
    op.create_index("ix_docker_commands_created_at", "docker_commands", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_docker_commands_created_at", table_name="docker_commands")
    op.drop_index("ix_docker_commands_server_id", table_name="docker_commands")
    op.drop_index("ix_docker_commands_lookup", table_name="docker_commands")
    op.drop_table("docker_commands")
