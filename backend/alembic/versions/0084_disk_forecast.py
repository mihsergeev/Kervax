"""Прогноз заполнения дисков и inode.

servers.disk_forecast - что насчитал планировщик по истории заполнения: когда при нынешнем
росте кончится место или inode на каждом разделе. Его читают алерт и карточка сервера.
"""

from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("disk_forecast", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("servers", "disk_forecast")
