"""Хронология сервера: что на нем менялось и что с ним было, одной лентой.

Сами пишем только то, чего больше нигде нет (таблица server_events):
- контейнеры: новый, удален, сменился образ, перезапущен или пересоздан, упал и поднялся по
  restart-политике, остановлен, запущен;
- система: перезагрузка, обновились пакеты, нужна перезагрузка после обновления, новое ядро,
  версии ОС, Docker и агента;
- порты: открылся, закрылся (ниже эфемерного диапазона).
Снимок для сравнения - в timeline_state. Появление и пропажа контейнера или порта
подтверждаются вторым отчетом подряд: при пересоздании контейнер на секунды пропадает, и без
этого вместо "пересоздан" были бы "удален" и "новый".

Остальное лента берет при чтении из того, что уже хранится: изменения доступа и кронов
(servers.changes), доставленные алерты, действия в панели, окна обслуживания, заметки.

Перед сбоем (сервер замолчал, сайт лег, 5xx, контейнер упал) в алерт дописывается, что менялось
на сервере за час до этого: так видно, что сломал деплой или обновление.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update

from app.models import Server, ServerEvent, ServerPackages, TimelineState

log = logging.getLogger("kervax.timeline")

KEEP = timedelta(days=90)
# сколько смотреть назад для строки "перед этим" в алерте
BEFORE = timedelta(minutes=60)
# порты от этого и выше - эфемерные, их открывает кто угодно на минуту
_EPHEMERAL = 32768
# порт, сменившийся столько раз за окно, дальше молчит в хронологии до конца окна: на одной ноде
# 123/udp открывался и закрывался 159 раз за 10 дней
_FLAP_MAX = 4
_FLAP_WINDOW = timedelta(hours=24)
# падения одного контейнера в пределах этого окна сливаются в одно событие со счетом
_CRASH_MERGE = timedelta(minutes=30)
# отчет старше - нода молчит, сравнивать нечего
_STALE = timedelta(minutes=5)
_TRACK_EVERY = timedelta(minutes=1)

# разовые контейнеры: docker run --rm со случайным именем и задачи CI-раннера
_ADHOC = re.compile(r"^(?:[a-z]+_[a-z]+\d*|runner-[a-z0-9]+-project-\d+-.*)$")
_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
          "month": 2592000, "year": 31536000}
_UP = re.compile(r"^up (?:(\d+)|about an?|less than a) (second|minute|hour|day|week|month|year)s?\b")

# ветки отчета, которые нужны хронологии
_PARTS = {
    "containers": ("docker", "containers"),
    "docker_v": ("docker", "version"),
    "listen": ("listen",),
    "procs": ("extras", "changes", "listen"),
    "os_upd": ("os_upd",),
    "os": ("os",),
    "pkgs": ("pkgs",),
}


def uptime(status) -> int | None:
    """Сколько контейнер работает, по строке docker ("Up 2 hours", "Up About a minute").
    Нижняя граница интервала: со временем только растет, сброс - значит перезапуск."""
    m = _UP.match(str(status or "").strip().lower())
    if not m:
        return None
    if m.group(1):
        n = int(m.group(1))
    else:
        n = 0 if "less than" in m.group(0) else 1
    return n * _UNITS[m.group(2)]


def _i(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _iso(ts) -> str:
    if isinstance(ts, datetime):
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return ""


def snapshot(row, rep: dict) -> dict:
    """Компактный снимок для сравнения. None в ветке - агент ее не прислал, не сравниваем."""
    cont = rep.get("containers")
    c = None
    if isinstance(cont, list):
        c = {}
        for x in cont:
            if not isinstance(x, dict) or not x.get("name") or _ADHOC.match(str(x["name"])):
                continue
            c[str(x["name"])] = {"i": str(x.get("image") or ""), "u": uptime(x.get("status")),
                                 "r": _i(x.get("restarts")) or 0, "s": str(x.get("state") or "")}
    p = None
    if isinstance(rep.get("listen"), list):
        procs = rep.get("procs") if isinstance(rep.get("procs"), dict) else {}
        p = {}
        for x in rep["listen"]:
            port = _i(x.get("port")) if isinstance(x, dict) else None
            if port is None or port <= 0 or port >= _EPHEMERAL:
                continue
            key, proc = f"{port}/{x.get('p') or 'tcp'}", str(procs.get(str(port)) or "")
            if _service(key, proc):
                p[key] = proc
    upd = rep.get("os_upd") if isinstance(rep.get("os_upd"), dict) else {}
    return {
        "ts": _iso(row.last_seen), "c": c, "p": p, "boot": _iso(row.rebooted_at),
        "k": str(upd.get("kernel") or ""), "kn": str(upd.get("kernel_new") or ""),
        "os": str(row.os or rep.get("os") or ""), "dk": str(rep.get("docker_v") or ""),
        "ag": str(row.agent_version or ""),
        "upd": _i(upd.get("updates")) if upd else None, "upd_ts": _i(upd.get("upd_ts")) or 0,
        "rb_ts": (_i(upd.get("reboot_ts")) or 0) if upd.get("reboot") else 0,
        "rb_pkgs": [str(x) for x in (upd.get("reboot_pkgs") or [])][:6], "ua": _i(upd.get("unatt_ts")) or 0,
        # агент присылает журнал установок: обновления видны по пакетам, счетчик не нужен
        "pkh": bool((rep.get("pkgs") or {}).get("log")) if isinstance(rep.get("pkgs"), dict) else False,
    }


def _service(key: str, proc: str) -> bool:
    """Слушающий сокет - сервис, а не клиентская сессия. UDP-сокет на порту от 1024 без известного
    процесса - сессия: xray и VPN открывают такой на каждую UDP-сессию клиента, живет он минуты, и
    на одной ноде это давало 52 тысячи строк "открыт/закрыт порт" за 10 дней."""
    port, _, proto = key.partition("/")
    return not (proto.startswith("udp") and port.isdigit() and int(port) >= 1024 and not proc)


def _ev(ts: str, cat: str, kind: str, obj: str, text: str) -> dict:
    return {"ts": ts, "cat": cat, "kind": kind, "obj": obj, "text": text}


def _confirm(prev: dict, cur: dict, pend: dict, key: str, ts: str) -> tuple[list, list, dict]:
    """Появилось и пропало, подтвержденное вторым снимком подряд. Возвращает (новые, пропавшие,
    подтвержденный набор). pend[key + "+"/"-"] - кандидаты с момента, когда их заметили."""
    plus, minus = dict(pend.get(key + "+") or {}), dict(pend.get(key + "-") or {})
    added, gone = [], []
    out = dict(prev)
    for k in cur.keys() - prev.keys():
        if k in plus:
            added.append((k, plus.pop(k)))
            out[k] = cur[k]
        else:
            plus[k] = ts
    for k in prev.keys() - cur.keys():
        if k in minus:
            gone.append((k, minus.pop(k)))
            out.pop(k, None)
        else:
            minus[k] = ts
    # кандидат, который вернулся как был, больше не кандидат
    pend[key + "+"] = {k: v for k, v in plus.items() if k in cur and k not in prev}
    pend[key + "-"] = {k: v for k, v in minus.items() if k in prev and k not in cur}
    return added, gone, out


def diff(prev: dict | None, cur: dict, pend: dict) -> tuple[list[dict], dict]:
    """События между снимками и новый подтвержденный снимок. Первый снимок - точка отсчета."""
    if not prev:
        return [], cur
    ts = cur["ts"]
    ev: list[dict] = []
    out = dict(cur)

    pc, cc = prev.get("c"), cur.get("c")
    if isinstance(pc, dict) and isinstance(cc, dict):
        added, gone, conf = _confirm(pc, cc, pend, "c", ts)
        for name, at in added:
            ev.append(_ev(at, "deploy", "new", name, f"новый контейнер {name} ({cc[name]['i']})"))
        for name, at in gone:
            ev.append(_ev(at, "deploy", "gone", name, f"контейнер {name} удален"))
        for name in pc.keys() & cc.keys():
            o, n = pc[name], cc[name]
            conf[name] = n
            restarted = o.get("u") is not None and n.get("u") is not None and n["u"] + 60 < o["u"]
            crashed = (n.get("r") or 0) > (o.get("r") or 0)
            if o.get("i") and n.get("i") and o["i"] != n["i"]:
                ev.append(_ev(ts, "deploy", "image", name, f"{name}: образ {o['i']} -> {n['i']}"))
            elif crashed:
                d = (n.get("r") or 0) - (o.get("r") or 0)
                ev.append(dict(_ev(ts, "crash", "crash", name, f"{name}: упал и перезапущен"), n=d))
            elif restarted:
                ev.append(_ev(ts, "deploy", "restart", name, f"{name}: перезапущен или пересоздан"))
            was, now_run = o.get("s") == "running", n.get("s") == "running"
            if was and not now_run and n.get("s") != "restarting":
                ev.append(_ev(ts, "state", "stop", name, f"{name}: остановлен ({n.get('s') or '?'})"))
            elif not was and now_run and o.get("s") != "restarting" and not restarted:
                ev.append(_ev(ts, "state", "start", name, f"{name}: запущен"))
        out["c"] = conf
    elif cc is None:
        out["c"] = pc  # агент не прислал контейнеры: держим прошлое, не "все удалены"

    pp, cp = prev.get("p"), cur.get("p")
    if isinstance(pp, dict) and isinstance(cp, dict):
        # снимки, сохраненные до правила _service, чистим тем же правилом: иначе все сессии из
        # них разом пришли бы строками "закрыт порт"
        pp = {k: v for k, v in pp.items() if _service(k, v)}
        cp = {k: v for k, v in cp.items() if _service(k, v)}
        cur["p"] = cp
        added, gone, conf = _confirm(pp, cp, pend, "p", ts)
        try:
            cutoff = (datetime.fromisoformat(ts) - _FLAP_WINDOW).isoformat()
        except ValueError:
            cutoff = ""
        flap = {k: [t for t in v if t >= cutoff] for k, v in (pend.get("pflap") or {}).items()}
        for key, at, kind, word, src in [(k, a, "open", "открыт", cp) for k, a in added] + \
                [(k, a, "close", "закрыт", pp) for k, a in gone]:
            hist = flap.setdefault(key, [])
            hist.append(at)
            if len(hist) == _FLAP_MAX:
                ev.append(_ev(at, "ports", "flap", key, f"порт {key} то открывается, то закрывается: "
                              "дальше в хронологию не пишется, пока не утихнет на сутки"))
            elif len(hist) < _FLAP_MAX:
                proc = src.get(key) or ""
                ev.append(_ev(at, "ports", kind, key, f"{word} порт {key}" + (f" ({proc})" if proc else "")))
        pend["pflap"] = {k: v for k, v in flap.items() if v}
        out["p"] = conf
    elif cp is None:
        out["p"] = pp

    if prev.get("boot") and cur.get("boot") and cur["boot"] != prev["boot"]:
        ev.append(_ev(cur["boot"], "system", "reboot", "", "перезагрузка"))
    for key, kind, label in (("k", "kernel", "ядро"), ("os", "os", "ОС"), ("dk", "docker", "Docker"),
                             ("ag", "agent", "агент Kervax")):
        a, b = prev.get(key) or "", cur.get(key) or ""
        if a and b and a != b:
            ev.append(_ev(ts, "system", kind, "", f"{label}: {a} -> {b}"))
    if cur.get("kn") and cur["kn"] != (prev.get("kn") or ""):
        ev.append(_ev(_iso(cur.get("upd_ts")) or ts, "system", "kernel_new", "",
                      f"установлено ядро {cur['kn']}, заработает после перезагрузки"))
    a, b = prev.get("upd"), cur.get("upd")
    if isinstance(a, int) and isinstance(b, int) and 0 <= b < a and cur.get("upd_ts") != prev.get("upd_ts") \
            and not cur.get("pkh"):
        # когда именно - точно не знаем: ставили между двумя подсчетами (раз в час). Если между
        # ними прошли автообновления - это они
        ua = cur.get("ua") or 0
        at = ua if (prev.get("upd_ts") or 0) < ua <= (cur.get("upd_ts") or 0) else cur.get("upd_ts")
        ev.append(_ev(_iso(at) or ts, "system", "packages", "",
                      f"обновлены пакеты: ждали обновления {a}, осталось {b}"))
    if cur.get("rb_ts") and cur["rb_ts"] != prev.get("rb_ts"):
        pk = cur.get("rb_pkgs") or []
        ev.append(_ev(_iso(cur["rb_ts"]), "system", "reboot_req", "",
                      "после обновления нужна перезагрузка" + (f": {', '.join(pk)}" if pk else "")))
    return ev, out


# --- запись ----------------------------------------------------------------------------------

_last_track: datetime | None = None
_last_prune: datetime | None = None


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


async def _store(session, sid: int, events: list[dict]) -> None:
    for e in events:
        if e["kind"] == "crash":
            # падения подряд - одно событие со счетом, иначе петля перезапусков забьет ленту
            last = (await session.execute(
                select(ServerEvent).where(ServerEvent.server_id == sid, ServerEvent.kind == "crash",
                                          ServerEvent.obj == e["obj"],
                                          ServerEvent.ts >= _dt(e["ts"]) - _CRASH_MERGE)
                .order_by(ServerEvent.ts.desc()).limit(1)
            )).scalar_one_or_none()
            if last is not None:
                last.n = (last.n or 1) + int(e.get("n") or 1)
                last.text = f"{e['obj']}: падал и перезапускался, раз: {last.n}"
                last.ts = _dt(e["ts"])
                continue
            n = int(e.get("n") or 1)
            if n > 1:
                e = dict(e, text=f"{e['obj']}: падал и перезапускался, раз: {n}")
        session.add(ServerEvent(server_id=sid, ts=_dt(e["ts"]), cat=e["cat"], kind=e["kind"],
                                obj=e["obj"][:255], text=e["text"], n=int(e.get("n") or 1)))


async def track(session_factory, now: datetime) -> int:
    """Свежие отчеты - в хронологию. Возвращает, сколько событий записано."""
    global _last_track, _last_prune
    if _last_track is not None and now - _last_track < _TRACK_EVERY:
        return 0
    _last_track = now
    cols = [Server.last_report[p if len(p) > 1 else p[0]] for p in _PARTS.values()]
    total = 0
    async with session_factory() as s:
        states = {sid: st for sid, st in (await s.execute(select(TimelineState.server_id, TimelineState.state))).all()}
        rows = (await s.execute(
            select(Server.id, Server.last_seen, Server.rebooted_at, Server.os, Server.agent_version, *cols)
            .where(Server.enabled.is_(True))
        )).all()
        for row in rows:
            seen = row.last_seen if row.last_seen is None or row.last_seen.tzinfo else \
                row.last_seen.replace(tzinfo=timezone.utc)
            if seen is None or now - seen > _STALE:
                continue
            st = states.get(row.id) if isinstance(states.get(row.id), dict) else None
            prev = (st or {}).get("snap")
            if prev and prev.get("ts") == _iso(seen):
                continue  # нового отчета нет
            rep = dict(zip(_PARTS, row[5:]))
            cur = snapshot(row, rep)
            pend = dict((st or {}).get("pend") or {})
            events, snap = diff(prev, cur, pend)
            new_state = {"snap": snap, "pend": pend}
            if st is None:
                s.add(TimelineState(server_id=row.id, state=new_state))
            else:
                await s.execute(update(TimelineState).where(TimelineState.server_id == row.id)
                                .values(state=new_state))
            if events:
                await _store(s, row.id, events)
                total += len(events)
        if _last_prune is None or now - _last_prune > timedelta(hours=1):
            _last_prune = now
            await s.execute(delete(ServerEvent).where(ServerEvent.ts < now - KEEP))
        await s.commit()
    return total


async def forget(session, server_id: int) -> None:
    """Сервер удален - его хронология тоже."""
    await session.execute(delete(ServerEvent).where(ServerEvent.server_id == server_id))
    await session.execute(delete(TimelineState).where(TimelineState.server_id == server_id))
    await session.execute(delete(ServerPackages).where(ServerPackages.server_id == server_id))


# --- "перед этим" для алертов ----------------------------------------------------------------

# что в строке "перед этим" не нужно: падения - это уже симптом, а не причина
_BEFORE_SKIP = {"crash"}
_BEFORE_LABEL = {
    "image": "новый образ", "restart": "перезапущен или пересоздан", "new": "новый контейнер",
    "gone": "удален контейнер", "stop": "остановлен", "start": "запущен", "open": "открыт порт",
    "close": "закрыт порт",
}
_CHANGE_LABEL = {"key": "SSH-ключи", "user": "пользователи", "sudo": "sudo", "cron": "кроны",
                 "unit": "юниты", "login": "вход по паролю"}


def before_text(events: list[dict], changes_log: list[dict], start: datetime, end: datetime,
                exclude_objs: set[str] | frozenset = frozenset(), limit: int = 4) -> str:
    """Строка "↳ за час до этого: ..." или пусто. events - строки server_events (dict),
    changes_log - servers.changes["log"]. Однотипное сворачивается: "перезапущены: a, b и еще 3"."""
    lo, hi = start.isoformat(), end.isoformat()
    groups: dict[str, list[str]] = {}
    order: list[str] = []

    def add(label: str, item: str) -> None:
        if label not in groups:
            groups[label] = []
            order.append(label)
        if item and item not in groups[label]:
            groups[label].append(item)

    for e in sorted(events, key=lambda x: x["ts"], reverse=True):
        if not (lo <= e["ts"] <= hi) or e["kind"] in _BEFORE_SKIP or (e.get("obj") and e["obj"] in exclude_objs):
            continue
        if e["kind"] in _BEFORE_LABEL:
            add(_BEFORE_LABEL[e["kind"]], e["obj"])
        else:
            add(e["text"], "")
    for e in changes_log or []:
        if not isinstance(e, dict) or not (lo <= str(e.get("ts") or "") <= hi):
            continue
        add(f"изменились {_CHANGE_LABEL.get(str(e.get('kind')), 'настройки доступа')}", "")
    if not order:
        return ""
    parts = []
    for label in order[:limit]:
        items = groups[label]
        if items:
            shown = ", ".join(items[:3]) + (f" и еще {len(items) - 3}" if len(items) > 3 else "")
            parts.append(f"{label}: {shown}")
        else:
            parts.append(label)
    if len(order) > limit:
        parts.append(f"и еще {len(order) - limit}")
    mins = max(1, round((end - start).total_seconds() / 60))
    head = "за час до этого" if mins == 60 else f"за {mins} мин до этого"
    return f"↳ {head}: " + "; ".join(parts)


async def events_between(session, server_ids, start: datetime, end: datetime) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    ids = list(server_ids)
    if not ids:
        return out
    for e in await session.scalars(
        select(ServerEvent).where(ServerEvent.server_id.in_(ids), ServerEvent.ts >= start, ServerEvent.ts <= end)
    ):
        out.setdefault(e.server_id, []).append(
            {"ts": _iso(e.ts), "cat": e.cat, "kind": e.kind, "obj": e.obj, "text": e.text})
    return out


# --- лента для карточки сервера ----------------------------------------------------------------

_VIEW_MAX = 1500
_ACCESS_KINDS = {"key", "user", "sudo", "login"}


def _alert_line(text: str, name: str) -> str:
    """Первая строка доставленного алерта без значка и имени сервера: в карточке они лишние."""
    line = (text or "").split("\n", 1)[0].strip()
    m = re.match(r"^\S*\s*" + re.escape(name) + r"\s+[—-]\s+", line)
    return line[m.end():] if m else line


async def view(session, srv, user, since: datetime, until: datetime, tz: str) -> dict:
    """Хронология сервера за [since, until]: свои события, доступ и кроны, алерты, действия в
    панели (только админу - как и сам журнал), окна обслуживания, заметка. Новые сверху."""
    from app import maint
    from app.models import AlertEvent, AuditLog, MaintenanceWindow

    out: list[dict] = []
    for e in await session.scalars(
        select(ServerEvent).where(ServerEvent.server_id == srv.id, ServerEvent.ts >= since,
                                  ServerEvent.ts <= until)
        .order_by(ServerEvent.ts.desc()).limit(_VIEW_MAX)
    ):
        out.append({"ts": _iso(e.ts), "cat": e.cat, "kind": e.kind, "obj": e.obj, "text": e.text, "n": e.n}
                   | ({"data": e.data} if e.data else {}))
    lo, hi = since.isoformat(), until.isoformat()
    ch = srv.changes if isinstance(getattr(srv, "changes", None), dict) else {}
    for e in ch.get("log") or []:
        if isinstance(e, dict) and lo <= str(e.get("ts") or "") <= hi:
            kind = str(e.get("kind") or "")
            out.append({"ts": str(e["ts"]), "cat": "access" if kind in _ACCESS_KINDS else "config",
                        "kind": kind, "text": str(e.get("text") or ""), "alert": bool(e.get("alert"))})
    for a in await session.scalars(
        select(AlertEvent).where(AlertEvent.target == srv.name, AlertEvent.section != "sites",
                                 AlertEvent.ts >= since, AlertEvent.ts <= until)
        .order_by(AlertEvent.ts.desc()).limit(_VIEW_MAX)
    ):
        out.append({"ts": _iso(a.ts), "cat": "alert", "kind": a.kind, "text": _alert_line(a.text, srv.name),
                    "recovery": bool(a.recovery)})
    if getattr(user, "role", "") == "admin":
        for a in await session.scalars(
            select(AuditLog).where(or_(AuditLog.target == srv.name, AuditLog.detail == f"srv={srv.id}"),
                                   AuditLog.ts >= since, AuditLog.ts <= until)
            .order_by(AuditLog.ts.desc()).limit(300)
        ):
            # действия с контейнером, подом, бэкапом пишутся по номеру сервера (srv=N), а объект
            # у них - сам контейнер или под: его и показываем
            by_id = a.detail == f"srv={srv.id}"
            out.append({"ts": _iso(a.ts), "cat": "panel", "kind": a.action, "user": a.username,
                        "text": ((a.target if by_id else a.detail) or "")[:300]})
    for w in await session.scalars(select(MaintenanceWindow)):
        if srv.id not in (w.server_ids or []):
            continue
        seen: set[tuple] = set()
        probe = since
        while probe < until + timedelta(days=7):
            for a, b in maint.occurrences(w, probe, tz):
                if a < until and b > since and (a, b) not in seen:
                    seen.add((a, b))
            probe += timedelta(days=7)
        for a, b in seen:
            out.append({"ts": _iso(a), "cat": "panel", "kind": "maint", "text": w.title, "end": _iso(b)})
    if getattr(srv, "note", "") and srv.note_at and since <= _dt(_iso(srv.note_at)) <= until:
        out.append({"ts": _iso(srv.note_at), "cat": "panel", "kind": "note", "user": srv.note_by or "",
                    "text": srv.note})
    out.sort(key=lambda x: x["ts"], reverse=True)
    out = out[:_VIEW_MAX]
    cats: dict[str, int] = {}
    for x in out:
        cats[x["cat"]] = cats.get(x["cat"], 0) + 1
    return {"since": since.isoformat(), "until": until.isoformat(), "events": out, "cats": cats}
