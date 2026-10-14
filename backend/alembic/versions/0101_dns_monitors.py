"""DNS-мониторы: запись домена в ожидаемое значение или без изменений с первого ответа."""

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checks", sa.Column("dns_type", sa.String(8), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("dns_expect", sa.Text(), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("dns_match", sa.String(8), nullable=False, server_default="equal"))
    op.add_column("checks", sa.Column("dns_server", sa.String(64), nullable=False, server_default=""))
    op.add_column("checks", sa.Column("dns_baseline", sa.JSON(), nullable=True))
    op.add_column("checks", sa.Column("dns_last", sa.JSON(), nullable=True))


def downgrade() -> None:
    for c in ("dns_last", "dns_baseline", "dns_server", "dns_match", "dns_expect", "dns_type"):
        op.drop_column("checks", c)
