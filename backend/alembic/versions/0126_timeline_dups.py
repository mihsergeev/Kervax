"""Хронология: убрать повторы "установлено ядро" и "нужна перезагрузка".

До 1.4.319 на каждом рестарте агента первый отчет приходил без обновлений ОС, и следующий
писал оба события заново: до 15 одинаковых строк на сервер за неделю. Оставляем первую из
одинаковых строк до следующей перезагрузки или смены ядра; строки с другим текстом (другое
ядро, другие пакеты) не трогаем.
"""

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_DUPS = ("reboot_req", "kernel_new")
# после них то же ожидание - уже новое
_RESET = ("reboot", "kernel")


def upgrade() -> None:
    bind = op.get_bind()
    ev = sa.table("server_events", sa.column("id", sa.Integer), sa.column("server_id", sa.Integer),
                  sa.column("kind", sa.String), sa.column("text", sa.Text))
    rows = bind.execute(sa.select(ev.c.id, ev.c.server_id, ev.c.kind, ev.c.text)
                        .where(ev.c.kind.in_(_DUPS + _RESET))
                        .order_by(ev.c.server_id, ev.c.id)).all()
    drop: list[int] = []
    seen: dict[int, set] = {}
    for rid, sid, kind, text in rows:
        said = seen.setdefault(sid, set())
        if kind in _RESET:
            said.clear()
        elif (kind, text) in said:
            drop.append(rid)
        else:
            said.add((kind, text))
    for i in range(0, len(drop), 500):
        bind.execute(sa.delete(ev).where(ev.c.id.in_(drop[i:i + 500])))


def downgrade() -> None:
    pass
