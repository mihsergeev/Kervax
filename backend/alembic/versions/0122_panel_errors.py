"""Журнал ошибок самой панели (app/panel_errors.py) и кто поставил команду агенту: провал
команды, которую запустил человек, попадает в журнал с его именем."""

import sqlalchemy as sa
from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

_CMD_TABLES = ("docker_commands", "kube_commands", "backup_commands")


def upgrade() -> None:
    op.create_table(
        "panel_errors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fp", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("username", sa.String(64), nullable=False, server_default=""),
        sa.Column("page", sa.String(300), nullable=False, server_default=""),
        sa.Column("section", sa.String(32), nullable=False, server_default=""),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("server_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("action", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.String(32), nullable=False, server_default=""),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("reopened", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_panel_errors_fp", "panel_errors", ["fp"])
    op.create_index("ix_panel_errors_kind", "panel_errors", ["kind"])
    op.create_index("ix_panel_errors_last_at", "panel_errors", ["last_at"])
    op.create_index("ix_panel_errors_username", "panel_errors", ["username"])
    op.create_index("ix_panel_errors_resolved", "panel_errors", ["resolved"])
    for t in _CMD_TABLES:
        op.add_column(t, sa.Column("created_by", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    for t in _CMD_TABLES:
        op.drop_column(t, "created_by")
    for ix in ("resolved", "username", "last_at", "kind", "fp"):
        op.drop_index(f"ix_panel_errors_{ix}", table_name="panel_errors")
    op.drop_table("panel_errors")
