"""Связь между серверами: агент сервера раз в 30 секунд трижды пробует открыть TCP-соединение
с заданными адресами (agent/links.go, агент 2.31) - вход VPN с выходом, приложение с базой.

Из отчета: сколько попыток прошло и время рукопожатия. Каждая порция - точка в link_samples
(график потерь и задержки). Состояние для алерта - доля неудачных попыток за последние
5 минут (ServerLink.win), а не за одну порцию: в порции всего три попытки, и одна неудача на
плохом канале то есть, то нет. Сводка по всем связям сервера - Server.links_state: по ней
алерт "Связь между серверами" и строка в "Что сломано".

Неудачная попытка - это когда за 3 секунды не прошли ни первый SYN, ни повтор через секунду,
т.е. потеряны два пакета подряд. Поэтому 20% неудачных попыток - уже очень плохой канал.

Связь с сервером панели, который сам сейчас молчит, не алертит: лежит он целиком, и про это
уже есть алерт "недоступен". Задержка не алертит: у каждой пары своя норма.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import delete, select, update

from app.models import Server, ServerLink, LinkSample

MAX_PER_SERVER = 16  # как linkMax в агенте: больше он и не проверит
FRESH = timedelta(minutes=3)  # ответ старше - агент связь не проверяет (старая версия, офлайн)
WINDOW_S = 300  # окно доли неудач для алерта
LOSS_DOWN = 100.0  # за все окно ни одна попытка не прошла
LOSS_WARN = 20.0
_HOST_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_-]{0,62})(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,62}))*\.?$")


def host_ok(host: str) -> bool:
    """Адрес связи: IP или имя хоста, без схемы, порта, пути и учетки."""
    h = (host or "").strip()
    if not h or len(h) > 253:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(_HOST_RE.match(h))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(v) -> float | None:
    return round(float(v), 1) if isinstance(v, (int, float)) and v >= 0 else None


def label(link: ServerLink, names: dict[int, str]) -> str:
    """Как связь называется в алерте и карточке: "kz-exit:443 (выход каскада)"."""
    base = names.get(link.target_server_id) if link.target_server_id else None
    s = f"{base or link.host}:{link.port}"
    return f"{s} ({link.note})" if link.note else s


def window_loss(win, now: datetime) -> tuple[float | None, int]:
    """Доля неудачных попыток за окно, % и сколько попыток в него попало."""
    t0 = now.timestamp() - WINDOW_S
    sent = ok = 0
    for x in win or []:
        if isinstance(x, list) and len(x) == 3 and x[0] >= t0:
            sent += int(x[1])
            ok += int(x[2])
    return (round((sent - ok) / sent * 100, 1) if sent else None), sent


async def target_names(session, rows, now: datetime) -> tuple[dict[int, str], set[int]]:
    """Имена серверов панели, с которыми связи, и какие из них сейчас молчат."""
    from app.collector import seen_online  # collector импортирует этот модуль

    tids = {r.target_server_id for r in rows if r.target_server_id}
    names: dict[int, str] = {}
    silent: set[int] = set()
    if tids:
        for sid, name, seen, off, en in (await session.execute(
            select(Server.id, Server.name, Server.last_seen, Server.offline_after_seconds, Server.enabled)
            .where(Server.id.in_(tids))
        )).all():
            names[sid] = name
            if en and seen is not None and not seen_online(
                    SimpleNamespace(last_seen=seen, offline_after_seconds=off or 120), now):
                silent.add(sid)
    return names, silent


async def handle(session, server: Server, results, now: datetime) -> list[dict]:
    """Результаты проверок из отчета - в связи и точки графика; в ответ - что проверять."""
    rows = list(await session.scalars(
        select(ServerLink).where(ServerLink.server_id == server.id).order_by(ServerLink.id)))
    if not rows:
        if server.links_state:
            server.links_state = None
        return []
    by = {r.id: r for r in rows}
    t0 = now.timestamp() - WINDOW_S
    for res in results or []:
        if not isinstance(res, dict):
            continue
        try:
            lid, sent, ok = int(res.get("id") or 0), int(res.get("sent") or 0), int(res.get("ok") or 0)
        except (TypeError, ValueError):
            continue
        row = by.get(lid)
        if row is None or not 0 < sent <= 1000:
            continue
        ok = max(0, min(ok, sent))
        loss = round((sent - ok) / sent * 100, 1)
        rtt, rmax = (_f(res.get("rtt_ms")), _f(res.get("rtt_max"))) if ok else (None, None)
        win = [x for x in (row.win or []) if isinstance(x, list) and len(x) == 3 and x[0] >= t0]
        win.append([int(now.timestamp()), sent, ok])
        row.win = win
        row.last_ts, row.last_loss = now, window_loss(win, now)[0]
        if rtt is not None:
            row.last_rtt = rtt
        if ok < sent:
            row.last_err = str(res.get("err") or "")[:200]
        elif row.last_loss == 0:
            row.last_err = ""
        session.add(LinkSample(link_id=row.id, ts=now, loss=loss, rtt=rtt, rtt_max=rmax))
    names, silent = await target_names(session, rows, now)
    state = []
    for r in rows:
        if r.last_ts is None or now - _aware(r.last_ts) > FRESH:
            continue
        state.append({"id": r.id, "name": label(r, names), "loss": r.last_loss, "rtt": r.last_rtt,
                      "err": r.last_err, "held": r.target_server_id in silent})
    server.links_state = state or None
    return [{"id": r.id, "host": r.host, "port": r.port} for r in rows[:MAX_PER_SERVER]]


def problems(state) -> list[tuple[int, int, str]]:
    """(уровень, id связи, текст) по сводке: нет связи - 2, теряет попытки - 1."""
    out: list[tuple[int, int, str]] = []
    for x in state or []:
        if not isinstance(x, dict) or x.get("held"):
            continue
        loss = x.get("loss")
        if not isinstance(loss, (int, float)):
            continue
        name = str(x.get("name") or "?")
        err = str(x.get("err") or "")
        if loss >= LOSS_DOWN:
            out.append((2, int(x.get("id") or 0), f"нет связи с {name}" + (f": {err}" if err else "")))
        elif loss >= LOSS_WARN:
            out.append((1, int(x.get("id") or 0), f"связь с {name} теряет {loss:.0f}% попыток подключения"
                        + (f" ({err})" if err else "")))
    out.sort(key=lambda p: -p[0])
    return out


async def forget(session, server_id: int) -> None:
    """Сервер удаляют: его связи с историей уходят, связи других с ним остаются по адресу."""
    ids = list(await session.scalars(select(ServerLink.id).where(ServerLink.server_id == server_id)))
    if ids:
        await session.execute(delete(LinkSample).where(LinkSample.link_id.in_(ids)))
        await session.execute(delete(ServerLink).where(ServerLink.id.in_(ids)))
    await session.execute(update(ServerLink).where(ServerLink.target_server_id == server_id)
                          .values(target_server_id=None))
