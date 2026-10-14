"""Пакеты на серверах (агент 2.28): список установленных и журнал установок.

Только данные - для поиска ("где старый openssl") и хронологии ("что обновили перед сбоем").
Ни алертов, ни пунктов "требует действий": агент без этой функции просто не присылает пакеты.

Список большой, а нода за DPI не протолкнет его одним запросом, поэтому в отчете только
отпечаток (pkgs.h). Не совпал с сохраненным (servers.pkg_hash) - панель отвечает want_pkgs, и
агент шлет список кусками в /api/agent/packages. Куски копятся в server_packages.pending,
последний собирает список целиком.

Журнал (pkg_ev) приходит в отчетах новыми строками. Прогон apt - десятки строк за минуту, в
хронологию он ложится одним событием "пакеты: обновлены (47): openssl, libc6 ...". Прогон,
разрезанный границей отчетов, склеивается с предыдущим событием. Агент при старте шлет
журнал за двое суток, уже виденное отсекается по server_packages.ev_ts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import ServerEvent, ServerPackages

MAX_PARTS = 80
MAX_ITEMS_PART = 600
MAX_EVENTS = 500
# строки журнала с паузой не больше этой - один прогон apt
RUN_GAP = 180
_ACT = {"install": "install", "upgrade": "upgrade", "remove": "remove", "purge": "remove"}
_LABEL = {"upgrade": "обновлены", "install": "установлены", "remove": "удалены"}
# старше этого журнал в хронологию не берем: она столько не хранит
_KEEP = timedelta(days=90)


async def _row(session, server_id: int) -> ServerPackages:
    row = await session.get(ServerPackages, server_id)
    if row is None:
        row = ServerPackages(server_id=server_id, ev_ts=0)
        session.add(row)
    return row


async def store_part(session, server, h: str, mgr: str, part: int, parts: int, items: list, now: datetime) -> bool:
    """Кусок списка. Возвращает True, когда список собран целиком."""
    row = await _row(session, server.id)
    pend = row.pending if isinstance(row.pending, dict) else None
    if not pend or pend.get("h") != h or pend.get("parts") != parts:
        pend = {"h": h, "parts": parts, "mgr": mgr, "got": {}}
    got = dict(pend.get("got") or {})
    got[str(part)] = [[str(x[0])[:200], str(x[1])[:200]] for x in items
                      if isinstance(x, (list, tuple)) and len(x) == 2]
    if len(got) < parts:
        row.pending = {**pend, "got": got}
        return False
    full = [it for i in range(parts) for it in got.get(str(i), [])]
    row.items, row.hash, row.mgr, row.n, row.ts, row.pending = full, h, mgr[:8], len(full), now, None
    server.pkg_hash = h
    return True


def _text(items: list[dict]) -> str:
    parts = []
    for act in ("upgrade", "install", "remove"):
        grp = [i for i in items if i["a"] == act]
        if not grp:
            continue
        if len(grp) <= 2:
            shown = ", ".join(
                f"{i['p']} {i['o']} -> {i['n']}" if act == "upgrade" else f"{i['p']} {i['n'] or i['o']}".strip()
                for i in grp)
        else:
            shown = ", ".join(i["p"] for i in grp[:4]) + (f" и еще {len(grp) - 4}" if len(grp) > 4 else "")
        parts.append(f"{_LABEL[act]} ({len(grp)}): {shown}")
    return "пакеты: " + "; ".join(parts)


def _merge(old: list[dict], new: list[dict]) -> list[dict]:
    """Один пакет в прогоне - одна строка: обновили дважды - от первой версии до последней,
    удалили и вычистили (remove + purge) - одно удаление."""
    by: dict[str, dict] = {}
    for i in old + new:
        cur = by.get(i["p"])
        if cur is None:
            by[i["p"]] = dict(i)
        elif i["a"] == "remove":
            by[i["p"]] = {"a": "remove", "p": i["p"], "o": cur.get("o") or i.get("o", ""), "n": ""}
        else:
            by[i["p"]] = {"a": cur["a"] if cur["a"] == "install" else i["a"], "p": i["p"],
                          "o": cur.get("o", ""), "n": i.get("n", "")}
    return list(by.values())


async def store_events(session, server, events: list, now: datetime) -> int:
    """Новые строки журнала - в хронологию прогонами. Возвращает, сколько строк взято."""
    row = await _row(session, server.id)
    last = int(row.ev_ts or 0)
    keys = set(row.ev_keys or [])
    floor = int((now - _KEEP).timestamp())
    fresh: list[dict] = []
    for e in sorted((e for e in events[:MAX_EVENTS] if isinstance(e, dict)), key=lambda x: int(x.get("t") or 0)):
        t, act = int(e.get("t") or 0), _ACT.get(str(e.get("a") or ""))
        name = str(e.get("p") or "")[:200]
        if not act or not name or t < floor or t > int(now.timestamp()) + 3600:
            continue
        key = f"{e.get('a')}|{name}|{e.get('o') or ''}|{e.get('n') or ''}"
        if t < last or (t == last and key in keys):
            continue
        if t > last:
            last, keys = t, set()
        keys.add(key)
        fresh.append({"t": t, "a": act, "p": name, "o": str(e.get("o") or "")[:120], "n": str(e.get("n") or "")[:120]})
    row.ev_ts, row.ev_keys = last, sorted(keys)[:300]
    if not fresh:
        return 0
    runs: list[list[dict]] = []
    for e in fresh:
        if runs and e["t"] - runs[-1][-1]["t"] <= RUN_GAP:
            runs[-1].append(e)
        else:
            runs.append([e])
    for run in runs:
        start = datetime.fromtimestamp(run[0]["t"], timezone.utc)
        end = datetime.fromtimestamp(run[-1]["t"], timezone.utc)
        items = [{k: v for k, v in e.items() if k != "t"} for e in run]
        prev = (await session.execute(
            select(ServerEvent).where(ServerEvent.server_id == server.id, ServerEvent.kind == "pkgs",
                                      ServerEvent.ts >= start - timedelta(seconds=RUN_GAP), ServerEvent.ts <= end)
            .order_by(ServerEvent.ts.desc()).limit(1)
        )).scalar_one_or_none()
        if prev is not None:
            merged = _merge(prev.data if isinstance(prev.data, list) else [], items)
            prev.data, prev.text, prev.n, prev.ts = merged, _text(merged), len(merged), end
            continue
        merged = _merge([], items)
        session.add(ServerEvent(server_id=server.id, ts=end, cat="system", kind="pkgs", obj="",
                                text=_text(merged), n=len(merged), data=merged))
    return len(fresh)


async def list_for(session, server_id: int, q: str = "", limit: int = 400) -> dict:
    row = await session.get(ServerPackages, server_id)
    if row is None or not row.items:
        return {"mgr": "", "n": 0, "ts": None, "items": [], "total": 0}
    toks = q.lower().split()
    items = [it for it in row.items if all(t in f"{it[0]} {it[1]}".lower() for t in toks)] if toks else row.items
    return {"mgr": row.mgr, "n": row.n, "ts": row.ts.isoformat() if row.ts else None,
            "items": items[:limit], "total": len(items)}
