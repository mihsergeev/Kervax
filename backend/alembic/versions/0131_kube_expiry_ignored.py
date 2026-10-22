"""Сроки кластера "не нужен, не следить": servers.kube_expiry_ignored.

Старые TLS-секреты, которые никто не использует, истекают и навсегда красят раздел "Сроки" и
главную. Отметка привязана к сроку и отпечатку: обновили секрет - снова следим.
"""

import sqlalchemy as sa
from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("kube_expiry_ignored", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "kube_expiry_ignored")
