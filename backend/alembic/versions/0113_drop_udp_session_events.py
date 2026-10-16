"""Хронология: убрать строки "открыт/закрыт порт" про UDP-сессии клиентов (порт от 1024 без
процесса), которые больше не пишутся (app/timeline.py, _service). На двух VPN-нодах их было
60 тысяч за 10 дней, без миграции они висели бы в ленте до срока хранения."""

import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

events = sa.table("server_events", sa.column("id", sa.Integer), sa.column("cat", sa.String),
                  sa.column("kind", sa.String), sa.column("obj", sa.String), sa.column("text", sa.Text))


def _session(obj: str, text: str) -> bool:
    port, _, proto = (obj or "").partition("/")
    return proto.startswith("udp") and port.isdigit() and int(port) >= 1024 and "(" not in (text or "")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(events.c.id, events.c.obj, events.c.text).where(
        events.c.cat == "ports", events.c.kind.in_(("open", "close")), events.c.obj.like("%/udp%"))).all()
    ids = [r.id for r in rows if _session(r.obj, r.text)]
    for i in range(0, len(ids), 1000):
        bind.execute(events.delete().where(events.c.id.in_(ids[i:i + 1000])))


def downgrade() -> None:
    pass  # удаленные строки не восстановить, да и не нужно
