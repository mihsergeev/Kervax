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

import html
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import tg_fmt
from app.models import Server, TgAlert

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
RECEIVED = "_listen_received"
MAX_AGE = 2 * STEP
_PROTOS = frozenset({"tcp", "tcp6", "udp", "udp6"})
_SKIP_PROCS = frozenset({"docker-proxy", "kube-proxy", "containerd", "containerd-shim", "rootlessport"})


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def inventory(rep: dict, *, explicit: bool = False) -> list[dict] | None:
    listen = (rep or {}).get("listen")
    if not isinstance(listen, list) or not (listen or explicit or rep.get(RECEIVED) is True):
        return None
    if any(not isinstance(sock, dict) or not isinstance(sock.get("p"), str)
           or sock["p"] not in _PROTOS or type(sock.get("port")) is not int
           or not 0 < sock["port"] < 65536 for sock in listen):
        return None
    return listen


def complete(srv: Server, now: datetime) -> bool:
    listen = inventory(srv.last_report or {})
    seen = srv.last_seen
    if seen is None or listen is None or len(listen) >= TRUNCATED:
        return False
    seen = seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
    return now - seen <= MAX_AGE


def is_listening(rep: dict, key: str) -> bool:
    return any(sock["p"].startswith("tcp") and key == f'tcp:{sock["port"]}'
               for sock in inventory(rep) or [])


def current(rep: dict) -> dict[str, dict] | None:
    """Слушающие TCP-порты сейчас: {"tcp:22": {"port": 22, "proc": "sshd"}}; None - данных нет
    (агент старше 2.26): тогда историю не трогаем."""
    listen = inventory(rep)
    if listen is None:
        return None
    procs = ((rep.get("extras") or {}).get("changes") or {}).get("listen")
    procs = procs if isinstance(procs, dict) else {}
    out: dict[str, dict] = {}
    for s in listen:
        if not s["p"].startswith("tcp"):
            continue
        port = s["port"]
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
        if last is None or (full and now - last > FORGET):
            continue
        e = dict(e)
        if full and not e.get("miss"):
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
    """Пропавшие порты без заглушенных ("listen:<порт>")."""
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
        out.append({"key": key, "port": port, "proc": proc, "miss": e["miss"],
                    "text": f"порт {port}{f' ({proc})' if proc else ''} слушался {_days(last - first)} и пропал "
                            f"{ago} назад: то, что его держало, не работает"})
    return out


def _gone_key(key: str) -> bool:
    return key == "listen_gone" or key.startswith("listen_gone_")


def retire(state: dict, key: str) -> dict:
    """Forget one missing listener and remove it from the alert's notified members."""
    history = dict(state["listen_hist"])
    del history[key]
    result = {**state, "listen_hist": history}
    for field in ("listen_gone_sig", "listen_gone_told"):
        if field in state:
            result[field] = [item for item in state[field] or [] if item != key]
    if not result.get("listen_gone_sig"):
        return {k: v for k, v in result.items() if not _gone_key(k)}
    return result


def merge_tick(before: dict, current_state: dict, proposed: dict) -> dict:
    """Merge the tick's changes with writes made while it was working."""
    result = dict(current_state)
    history_changed = before.get("listen_hist") != current_state.get("listen_hist")
    for key in before.keys() | proposed.keys():
        if history_changed and (key == "listen_hist" or _gone_key(key)):
            continue
        if key not in proposed:
            result.pop(key, None)
        elif key not in before or before[key] != proposed[key]:
            result[key] = proposed[key]
    return result


def matching(state: dict, episodes: dict[str, str]) -> dict[str, str]:
    history = state.get("listen_hist") or {}
    return {key: miss for key, miss in episodes.items() if (history.get(key) or {}).get("miss") == miss}


def can_notify(srv: Server | None, episodes: dict[str, str], now: datetime) -> bool:
    return (srv is not None and srv.enabled and complete(srv, now)
            and matching(srv.alert_state or {}, episodes) == episodes
            and not any(is_listening(srv.last_report or {}, key) for key in episodes))


def mark_delivered(state: dict, episodes: dict[str, str], now: datetime) -> dict:
    """Commit the notification before a concurrent retirement reads its state."""
    keys = episodes.keys()
    return {**state, "listen_gone": 1,
            "listen_gone_sig": sorted(set(state.get("listen_gone_sig") or []) | keys),
            "listen_gone_told": sorted(set(state.get("listen_gone_told") or []) | keys),
            "listen_gone_said": now.isoformat()}


def closure(target: str, ports: str) -> str:
    if ", " in ports:
        return f"{target}: порты {ports} больше не нужны, алерт закрыт"
    return f"{target}: порт {ports} больше не нужен, алерт закрыт"


def tg_items(row: TgAlert, srv: Server, now: datetime) -> list[dict]:
    undo = row.undo or {}
    episodes = undo.get("listen_episodes")
    items = [item for item in problems(srv, now)
             if not (complete(srv, now) and is_listening(srv.last_report or {}, item["key"]))]
    if isinstance(episodes, dict):
        return [item for item in items if episodes.get(item["key"]) == item["miss"]]
    # Legacy rows carry only port keys. An episode that began after the message is unrelated.
    ts = row.ts
    if ts is None:
        return []
    ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    keys = undo.get("keys") or []
    return [item for item in items if MUTE_PREFIX + item["port"] in keys and _ts(item["miss"]) <= ts]


def tg_prune(row: TgAlert, groups: list[tuple[Server, list[dict]]], now: datetime) -> bool:
    """Keep only the current episodes in a partially closed Telegram message."""
    keys = {MUTE_PREFIX + item["port"] for _, items in groups for item in items}
    if not keys:
        row.resolved_at = now
        return False
    undo = row.undo or {}
    old_keys = set(undo.get("keys") or [])
    if old_keys != keys or "," in row.ref:
        row.undo = {**undo, "keys": sorted(keys)}
        if len(groups) == 1:
            srv, items = groups[0]
            if "," not in row.ref:
                row.undo["listen_episodes"] = {item["key"]: item["miss"] for item in items}
            what, facts = tg_short(items)
            row.text = tg_fmt.render("⚠️🔌", html.escape(srv.name), what, facts)
        else:
            facts = [f'{srv.name}: {tg_fmt.names([item["port"] for item in items])}' for srv, items in groups]
            row.text = tg_fmt.render("⚠️🔌", "", "Пропали порты", facts)
    return True


async def retire_tg(session, srv: Server, now: datetime) -> None:
    ref = f"srv:{srv.id}"
    rows = await session.scalars(select(TgAlert).where(
        TgAlert.kind == "listen_gone", TgAlert.resolved_at.is_(None),
        (TgAlert.ref == ref) | (TgAlert.ref.like("srv:%,%")),
    ).order_by(TgAlert.id).with_for_update())
    for row in rows:
        ids = [int(x) for x in row.ref[4:].split(",") if x.isdigit()]
        if srv.id not in ids:
            continue
        groups = []
        for sid in ids:
            source = srv if sid == srv.id else await session.get(Server, sid)
            if source is not None and (items := tg_items(row, source, now)):
                groups.append((source, items))
        tg_prune(row, groups, now)


def tg_short(items: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт listen_gone (вход - problems()): "Пропал порт 5432", "Слушал: postgres"."""
    if not items:
        return None
    ports = [str(x.get("port") or "?") for x in items]
    procs = [str(x.get("proc") or "") for x in items if x.get("proc")]
    if len(items) == 1:
        what = f"Пропал порт {ports[0]}"
        facts = [tg_fmt.fact("Слушал", procs[0])] if procs else []
    else:
        what = f"Пропали порты {tg_fmt.names(ports)}"
        facts = [tg_fmt.fact("Слушали", tg_fmt.names(procs))] if procs else []
    return what, facts
