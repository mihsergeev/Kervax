"""NOT NULL там, где его ждет модель: created_at у очередей команд и окон, oom_events.ts, oom_kills.data.

Модель считала эти колонки обязательными, а миграции создали их NULL. Значение всегда ставит
server_default или код, но строка с NULL created_at не попадала бы под чистку по
`created_at < floor` и жила бы вечно. Пустые значения, если вдруг есть, заполняются: время -
текущим (строка уйдет со следующей чисткой), data - пустым объектом. После этой миграции
`alembic check` на PostgreSQL чистый, и CI сверяет модели со схемой.
"""

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None

_TS = [
    ("backup_commands", "created_at"),
    ("backup_setup_jobs", "created_at"),
    ("docker_commands", "created_at"),
    ("kube_commands", "created_at"),
    ("maintenance_windows", "created_at"),
    ("oom_events", "ts"),
]


def upgrade() -> None:
    for table, col in _TS:
        op.execute(f"UPDATE {table} SET {col} = now() WHERE {col} IS NULL")
        op.alter_column(table, col, existing_type=sa.DateTime(timezone=True), nullable=False)
    op.execute("UPDATE oom_kills SET data = '{}' WHERE data IS NULL")
    op.alter_column("oom_kills", "data", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    op.alter_column("oom_kills", "data", existing_type=sa.JSON(), nullable=True)
    for table, col in reversed(_TS):
        op.alter_column(table, col, existing_type=sa.DateTime(timezone=True), nullable=True)
