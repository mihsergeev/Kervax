"""Ответы 5xx веб-сервера: своя точка в тайм-серии рядом с запросами.

Синтетический монитор видит только «сайт открылся». Доля ошибок в 0.1-0.3% - а именно
столько давал сломанный DNS кластера балансеров - для него невидима, потому что попадает
в один запрос из трёхсот. Хелпер уже читает access-логи ради счёта запросов, поэтому код
ответа берётся из той же строки.
"""

from alembic import op
import sqlalchemy as sa

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("web_5xx", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "web_5xx")
