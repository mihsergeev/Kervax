"""Разбивка веб-трафика для графиков: самые нагруженные логи и 5xx по кодам.

Графики раздела "Веб" были одной линией: по ней не видно, какой сайт вырос и какими
кодами идут ошибки. Теперь они стеком, как состав CPU.
"""

from alembic import op
import sqlalchemy as sa

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("web_top", sa.JSON(), nullable=True))
    op.add_column("server_metrics", sa.Column("web_codes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "web_codes")
    op.drop_column("server_metrics", "web_top")
