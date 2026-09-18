"""Запросы в минуту у веб-сервера ноды: своя точка в тайм-серии.

Наплыв трафика виден по байтам и соединениям только косвенно: 2.3 млн переходов в сутки
на fi-hz-aff это редиректы по 300 байт, канал почти не шевелится. Хелпер webserver-setup
0.7 считает строки access-логов раз в минуту, и панель хранит сумму рядом с остальными
метриками - чтобы был график и чтобы алерт мог сказать "запросов x40 к обычному".
"""

from alembic import op
import sqlalchemy as sa

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("web_rpm", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "web_rpm")
