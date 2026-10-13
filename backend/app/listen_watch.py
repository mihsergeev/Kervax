"""Пропал порт, который слушался неделями: сервис тихо умер вне systemd и Docker.

Упавший юнит видно (units), упавший контейнер тоже (docker_down), а скрипт под nohup, в screen
или под supervisor умирает молча: процесса нет, порт никто не слушает, на дашборде все зелено.
Слушающие порты агент шлет давно (2.26), панель ведет их историю в alert_state["listen_hist"]:
{"tcp:<порт>": {"first": когда впервые, "last": когда последний раз, "proc": кто слушал,
"miss": первый отчет, где порта не было}}.

Пропавшим считаем порт, который слушался не меньше STABLE подряд и не виден дольше GONE по
отчетам: отсчет идет от первого отчета без порта, а не от последнего с ним, иначе после
перезагрузки, пока сервис еще стартует, алерт пришел бы с первым же отчетом. Порт, которого не
было дольше RESET, начинает стаж заново: случайный порт раз в неделю - не "слушался неделю".
Динамические порты (32768 и выше) и порты, за которыми docker-proxy или kube-proxy, не считаем:
это случайные номера и опубликованные порты контейнеров, их судьбу видят проверки Docker и
Kubernetes. Через FORGET после пропажи порт снимаем с наблюдения.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models import Server

STABLE = timedelta(days=7)
GONE = timedelta(minutes=15)
FORGET = timedelta(days=7)
RESET = timedelta(days=1)
# агент шлет не больше 150 сокетов в порядке /proc: на полном списке "нет порта" не значит "пропал"
TRUNCATED = 150
# "last" живого порта обновляем не чаще раза в STEP: иначе alert_state переписывался бы каждый тик
STEP = timedelta(minutes=5)
EPHEMERAL = 32768
MUTE_PREFIX = "listen:"
_SKIP_PROCS = frozenset({"docker-proxy", "kube-proxy", "containerd", "containerd-shim", "rootlessport"})


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def current(rep: dict) -> dict[str, dict] | None:
    """Слушающие TCP-порты сейчас: {"tcp:22": {"port": 22, "proc": "sshd"}}; None - данных нет
    (агент старше 2.26): тогда историю не трогаем."""
    listen = (rep or {}).get("listen")
    if not isinstance(listen, list):
        return None
    procs = ((rep.get("extras") or {}).get("changes") or {}).get("listen")
    procs = procs if isinstance(procs, dict) else {}
    out: dict[str, dict] = {}
    for s in listen:
        if not isinstance(s, dict) or not str(s.get("p") or "").startswith("tcp"):
            continue
        try:
            port = int(s.get("port") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 < port < EPHEMERAL:
            continue
        proc = str(procs.get(str(port)) or "")
        if proc in _SKIP_PROCS:
            continue
        out[f"tcp:{port}"] = {"port": port, "proc": proc or out.get(f"tcp:{port}", {}).get("proc", "")}
    return out


def update(state: dict | None, rep: dict, now: datetime) -> dict | None:
    """Новое alert_state["listen_hist"]; None - менять нечего (нет данных о портах)."""
    cur = current(rep)
    if cur is None:
        return None
    old = state if isinstance(state, dict) else {}
    full = len(rep.get("listen") or []) < TRUNCATED
    iso = now.isoformat()
    new: dict[str, dict] = {}
    for key, e in old.items():
        if not isinstance(e, dict) or key in cur:
            continue
        last = _ts(e.get("last"))
        if last is None or now - last > FORGET:
            continue
        e = dict(e)
        if not full:
            e.pop("miss", None)  # список обрезан: про этот порт ничего не знаем
        elif not e.get("miss"):
            e["miss"] = iso
        new[key] = e  # пропал недавно - помним, чтобы сказать
    for key, x in cur.items():
        e = old.get(key) if isinstance(old.get(key), dict) else {}
        miss, last = _ts(e.get("miss")), _ts(e.get("last"))
        if (miss is None and last is not None and e.get("first") and now - last < STEP
                and (not x["proc"] or x["proc"] == e.get("proc"))):
            new[key] = e
            continue
        first = e.get("first") if miss is None or now - miss < RESET else None
        new[key] = {"first": first or iso, "last": iso, "proc": x["proc"] or e.get("proc") or ""}
    return new


def _days(d: timedelta) -> str:
    n = d.days
    return f"{n} дн" if n else f"{int(d.total_seconds() // 3600)} ч"


def problems(srv: Server, now: datetime) -> list[dict]:
    """Пропавшие порты: {"key", "port", "proc", "text"} без заглушенных ("listen:<порт>")."""
    st = (srv.alert_state or {}).get("listen_hist")
    if not isinstance(st, dict):
        return []
    muted = {m[len(MUTE_PREFIX):] for m in (srv.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    out = []
    for key, e in sorted(st.items()):
        if not isinstance(e, dict):
            continue
        first, last, miss = _ts(e.get("first")), _ts(e.get("last")), _ts(e.get("miss"))
        if first is None or last is None or miss is None or now - miss < GONE or last - first < STABLE:
            continue
        port = key.split(":", 1)[-1]
        if port in muted:
            continue
        proc = str(e.get("proc") or "")
        mins = int((now - miss).total_seconds() // 60)
        ago = f"{mins} мин" if mins < 120 else f"{mins // 60} ч"
        out.append({"key": key, "port": port, "proc": proc,
                    "text": f"порт {port}{f' ({proc})' if proc else ''} слушался {_days(last - first)} и пропал "
                            f"{ago} назад: то, что его держало, не работает"})
    return out
