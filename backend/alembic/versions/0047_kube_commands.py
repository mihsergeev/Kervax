"""kube_commands — очередь k8s-действий (rollout_restart/delete_pod/logs)

Revision ID: 0047
Revises: 0046
Create Date: 2026-07-18

"""
from alembic import op
import sqlalchemy as sa

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kube_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("ns", sa.String(length=253), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="pod"),
        sa.Column("name", sa.String(length=253), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("tail", sa.Integer(), nullable=False, server_default="400"),
        sa.Column("since", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=False, server_default=""),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_kube_commands_lookup", "kube_commands", ["server_id", "status"])
    op.create_index("ix_kube_commands_server_id", "kube_commands", ["server_id"])
    op.create_index("ix_kube_commands_created_at", "kube_commands", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_kube_commands_created_at", table_name="kube_commands")
    op.drop_index("ix_kube_commands_server_id", table_name="kube_commands")
    op.drop_index("ix_kube_commands_lookup", table_name="kube_commands")
    op.drop_table("kube_commands")
