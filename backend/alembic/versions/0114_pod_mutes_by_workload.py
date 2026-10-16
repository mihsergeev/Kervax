"""Заглушенные перезапуски подов - на ворклоад, а не на под: "restarts:pod:<ns>/<под>" ->
"restarts:pod:<ns>/<деплоймент>". Новые мьюты так и ставятся (app/podnames.py), а старые висели в
"Заглушено" именами подов с хэшами, по строке на под, и после деплоя ничего не значили."""

import re

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

# те же суффиксы, что в app/podnames.py: миграция не должна зависеть от того, как модуль поменяют
_A = "bcdfghjklmnpqrstvwxz2456789"
_RX = (re.compile(rf"^(.+)-[{_A}]{{6,10}}-[{_A}]{{5}}$"), re.compile(rf"^(.+)-[{_A}]{{5}}$"),
       re.compile(r"^(.+)-\d+$"))
_P = "restarts:pod:"

servers = sa.table("servers", sa.column("id", sa.Integer), sa.column("alert_mutes", sa.JSON),
                   sa.column("alert_snoozes", sa.JSON))


def _wk(key: str) -> str:
    if not key.startswith(_P):
        return key
    ns, _, name = key[len(_P):].partition("/")
    for rx in _RX:
        m = rx.match(name)
        if m:
            return f"{_P}{ns}/{m.group(1)}"
    return key


def upgrade() -> None:
    bind = op.get_bind()
    for sid, mutes, snoozes in bind.execute(sa.select(servers.c.id, servers.c.alert_mutes, servers.c.alert_snoozes)).all():
        values: dict = {}
        if isinstance(mutes, list) and any(isinstance(k, str) and k.startswith(_P) for k in mutes):
            new = list(dict.fromkeys(_wk(k) if isinstance(k, str) else k for k in mutes))
            if new != mutes:
                values["alert_mutes"] = new
        if isinstance(snoozes, dict) and any(k.startswith(_P) for k in snoozes):
            new_s: dict = {}
            for k, until in snoozes.items():
                w = _wk(k)
                new_s[w] = max(new_s[w], until) if w in new_s and isinstance(until, str) else until
            if new_s != snoozes:
                values["alert_snoozes"] = new_s
        if values:
            bind.execute(servers.update().where(servers.c.id == sid).values(**values))


def downgrade() -> None:
    pass  # имена подов уже не восстановить, да и поды те давно пересозданы
