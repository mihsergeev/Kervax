"""Сроки сертификата и домена: алерт за 7 и за 1 день, а не за 14/30.

За месяц до срока это не повод для алерта и не "Что сломано": срок виден в списке сайтов и в
карточке монитора, а напоминают за неделю и за день, как у сроков Kubernetes. Пороги по
умолчанию ([14, 7, 1] у сертификата, [30, 7, 1] у домена) становятся [7, 1]; свои значения
мониторов не трогаем. Напоминание, уже отправленное за 14 или 30 дней, забываем: иначе первый же
проход увидел бы срок "за порогами" и прислал "домен продлен", хотя его никто не продлевал.
"""

import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None

checks = sa.table(
    "checks",
    sa.column("id", sa.Integer),
    sa.column("ssl_warn_days", sa.JSON),
    sa.column("domain_warn_days", sa.JSON),
    sa.column("ssl_alerted_days", sa.Integer),
    sa.column("domain_alerted_days", sa.Integer),
)
NEW = [7, 1]
OLD = {"ssl_warn_days": [14, 7, 1], "domain_warn_days": [30, 7, 1]}
ALERTED = {"ssl_warn_days": "ssl_alerted_days", "domain_warn_days": "domain_alerted_days"}


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(checks)).mappings().all()
    for r in rows:
        values: dict = {}
        for col, old in OLD.items():
            if list(r[col] or []) != old:
                continue
            values[col] = NEW
            al = r[ALERTED[col]]
            if al is not None and al > max(NEW):
                values[ALERTED[col]] = None
        if values:
            bind.execute(checks.update().where(checks.c.id == r["id"]).values(**values))


def downgrade() -> None:
    bind = op.get_bind()
    for r in bind.execute(sa.select(checks)).mappings().all():
        values = {col: old for col, old in OLD.items() if list(r[col] or []) == NEW}
        if values:
            bind.execute(checks.update().where(checks.c.id == r["id"]).values(**values))
