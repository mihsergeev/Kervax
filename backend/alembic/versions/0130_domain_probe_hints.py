"""Типовой путь для найденного домена без главной: domain_probes.ext_hint, local_paths.

Домен API, MCP-сервера или бэкенда за прокси на "/" отвечает 404, и мастер показывал его
мертвым. Теперь панель пробует короткий список типовых путей (/health, /api, /mcp ...) и
предлагает мониторить тот, что отвечает.
"""

import sqlalchemy as sa
from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("domain_probes", sa.Column("ext_hint", sa.JSON(), nullable=True))
    op.add_column("domain_probes", sa.Column("local_paths", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("domain_probes", "local_paths")
    op.drop_column("domain_probes", "ext_hint")
