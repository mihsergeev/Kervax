"""Авто-очистка диска по галочке сервера и происхождение backup-команды.

servers.disk_autofix - галочка (по умолчанию выключена), servers.disk_autofix_state - когда
какое действие запускалось; backup_commands.origin = "auto" у команд планировщика: по нему
результат уходит уведомлением в каналы алертов.
"""

from alembic import op
import sqlalchemy as sa

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("disk_autofix", sa.Boolean(), nullable=False,
                                       server_default="0"))
    op.add_column("servers", sa.Column("disk_autofix_state", sa.JSON(), nullable=True))
    op.add_column("backup_commands", sa.Column("origin", sa.String(16), nullable=False,
                                               server_default=""))


def downgrade() -> None:
    op.drop_column("backup_commands", "origin")
    op.drop_column("servers", "disk_autofix_state")
    op.drop_column("servers", "disk_autofix")
