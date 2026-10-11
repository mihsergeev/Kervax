"""Что открыто в интернет.

servers.exposure - последняя внешняя проверка портов ноды: когда, откуда (локация-прокси или сама
панель), какие публичные адреса и порты проверены и что из них открыто, а что закрыто снаружи.
"""

from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("exposure", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "exposure")
