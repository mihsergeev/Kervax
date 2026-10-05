"""Ноды кластера, на которых агент не нужен.

servers.kube_node_ignored - имена нод кластера этого сервера, которые не надо подсвечивать
как "без агента" (чужая нода, временная, агент туда ставить нельзя). Остальные ноды без
агента панель выносит в "Требует действий".
"""

from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("kube_node_ignored", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "kube_node_ignored")
