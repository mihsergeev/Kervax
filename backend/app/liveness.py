"""Недоступен: молчит агент или сам сервер.

Алерт "недоступен" срабатывает, когда замолчал агент, и по нему не понять, что чинить: агент
(упал, не может достучаться до панели) или сам сервер (лежит, нет сети). В момент алерта
панель стучится к серверу сама:
- порт открыт - машина жива, молчит агент или его связь с панелью;
- отказ (RST) на порту, который был открыт, - адрес отвечает, машина в сети, но сервис на порту
  не работает: похоже на перезагрузку;
- ни ответа, ни отказа - лежит сервер или сеть до него. Если порты закрыты для панели
  файрволом, такая проверка ничего не доказывает, это сказано в тексте.

Эталон: раз в сутки, пока сервер на связи, панель проверяет то же самое и запоминает
(servers.liveness_base). На проде из 43 серверов до 9 панель не достукивается и на связи: порты
закрыты для нее. У таких "не отвечает" ничего не значит, и алерт говорит, что проверить нельзя. А
если на связи отвечал, а теперь молчит, - лежит сервер или сеть, без оговорок.

Куда стучаться: порты, открытые снаружи по последней проверке (app/open_ports.py), на адресах,
где отвечает именно эта нода (адрес шлюза NAT с чужим SSH отброшен, open_ports.foreign), плюс SSH
на собственных публичных адресах ноды и на адресе, заданном вручную (agent_ip). По частному
адресу у панели может отвечать другая машина, поэтому у SSH сверяется версия OpenSSH в баннере
с той, что у sshd ноды."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import open_ports
from app.models import Server

log = logging.getLogger("kervax.liveness")

_MAX = 6
BASE_EVERY = timedelta(hours=24)
_LOOK_EVERY = timedelta(minutes=10)
_BASE_BATCH = 12
_running: asyncio.Task | None = None
_last_start: datetime | None = None


def candidates(srv) -> list[tuple[str, int, bool]]:
    """(адрес, порт, сверять баннер SSH) - не больше _MAX, сначала проверенные снаружи."""
    rep = srv.last_report or {}
    bad = set(open_ports.foreign(srv))
    ssh = sorted({int(p) for p in open_ports._ssh_block(srv).get("ports") or [] if str(p).isdigit()} or {22})
    out: list[tuple[str, int, bool]] = []
    seen: set[tuple[str, int]] = set()

    def add(ip: str, port: int, check: bool) -> None:
        if ip and (ip, port) not in seen and len(out) < _MAX:
            seen.add((ip, port))
            out.append((ip, port, check))

    # что отвечало, пока сервер был на связи, - первым
    base = getattr(srv, "liveness_base", None)
    if isinstance(base, dict) and base.get("where") and ":" in str(base["where"]):
        ip, _, port = str(base["where"]).rpartition(":")
        if port.isdigit() and ip not in bad:
            add(ip, int(port), int(port) in ssh)
    for x in open_ports._ex(srv).get("ports") or []:
        if isinstance(x, dict) and x.get("state") == "open" and x.get("ip") not in bad:
            add(str(x["ip"]), int(x["port"]), int(x["port"]) in ssh)
    for ip in rep.get("ips") or []:
        if isinstance(ip, str) and open_ports.is_public(ip) and ip not in bad:
            for p in ssh:
                add(ip, p, True)
    manual = str(getattr(srv, "agent_ip", "") or "")
    if manual and manual not in bad:
        for p in ssh:
            add(manual, p, True)
    return out


def reachable(base) -> bool | None:
    """Достукивалась ли панель до сервера, пока он был на связи; None - эталона еще нет."""
    if not isinstance(base, dict) or "state" not in base:
        return None
    return base["state"] in ("alive", "refused")


async def check(srv, offline: bool = True) -> dict | None:
    """{state: alive|refused|silent|blind, where: "адрес:порт", tried: [...]} или None, если
    стучаться некуда (агент не прислал ни адресов, ни портов). blind - сервер молчит, но панель и на
    связи до него не достукивалась, так что молчание ничего не значит. Стучимся все равно: эталон
    мог попасть на сбой сети, а ответ сервера полезен и тогда."""
    pairs = candidates(srv)
    if not pairs:
        return None
    base = getattr(srv, "liveness_base", None)
    node = open_ports._ssh_ver(str(open_ports._ssh_block(srv).get("banner") or ""))
    states = await asyncio.gather(*(open_ports.probe(ip, port, None) for ip, port, _c in pairs))
    alive, refused = [], []
    for (ip, port, chk), st in zip(pairs, states):
        where = f"{ip}:{port}"
        if st == "open":
            if chk and node:
                outside = open_ports._ssh_ver(await open_ports.ssh_banner(ip, port, None))
                if outside and outside != node:
                    continue  # отвечает другая машина
            alive.append(where)
        elif st == "closed":
            refused.append(where)
    tried = [f"{ip}:{port}" for ip, port, _c in pairs]
    if alive:
        return {"state": "alive", "where": alive[0], "tried": tried}
    if refused:
        return {"state": "refused", "where": refused[0], "tried": tried}
    out = {"state": "silent", "where": "", "tried": tried}
    if offline and reachable(base) is False:
        out["state"] = "blind"
    elif offline and reachable(base):
        out["was"] = str(base.get("where") or "")  # на связи отвечал: молчание - не файрвол
    return out


def text(res: dict) -> str:
    st = res.get("state")
    if st == "alive":
        return (f"с панели сервер отвечает ({res['where']}): машина жива, молчит агент или его связь с "
                "панелью - systemctl status kervax-agent")
    if st == "refused":
        return (f"адрес отвечает отказом ({res['where']}): машина в сети, но порт не слушается - похоже "
                "на перезагрузку или упавший сервис")
    if st == "blind":
        return "проверить с панели нельзя: и пока сервер на связи, панель до него не достукивается"
    tried = ", ".join(res.get("tried") or [])
    if res.get("was"):
        return (f"с панели сервер не отвечает ({tried}), хотя на связи отвечал ({res['was']}): лежит сам "
                "сервер или сеть до него")
    return (f"с панели сервер не отвечает ({tried}): лежит сам сервер или сеть до него. Если эти порты "
            "закрыты для панели файрволом, проверка ничего не говорит")


async def _base_due(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Эталон для серверов на связи, у кого его нет или он старше BASE_EVERY."""
    async with session_factory() as s:
        servers = list(await s.scalars(select(Server).where(Server.enabled.is_(True))))
    due = []
    for srv in servers:
        if srv.last_seen is None:
            continue
        seen = srv.last_seen if srv.last_seen.tzinfo else srv.last_seen.replace(tzinfo=timezone.utc)
        if now - seen > timedelta(minutes=3) or not candidates(srv):
            continue
        base = srv.liveness_base if isinstance(srv.liveness_base, dict) else {}
        try:
            at = datetime.fromisoformat(str(base.get("ts") or ""))
        except ValueError:
            at = None
        if at is None or now - at >= BASE_EVERY:
            due.append((at or datetime.min.replace(tzinfo=timezone.utc), srv))
    due.sort(key=lambda x: x[0])
    batch = [srv for _at, srv in due[:_BASE_BATCH]]
    if not batch:
        return 0
    res = await asyncio.gather(*(check(srv, offline=False) for srv in batch), return_exceptions=True)
    async with session_factory() as s:
        for srv, r in zip(batch, res):
            if isinstance(r, Exception) or r is None:
                continue
            row = await s.get(Server, srv.id)
            if row is not None:
                row.liveness_base = {"state": r["state"], "where": r["where"], "ts": now.isoformat()}
        await s.commit()
    return len(batch)


async def start_baseline(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Эталон фоном, как внешняя проверка портов: тик планировщика его не ждет (закрытый для
    панели порт отвечает только таймаутом)."""
    global _running, _last_start
    if _running is not None and not _running.done():
        return
    if _last_start is not None and now - _last_start < _LOOK_EVERY:
        return
    _last_start = now

    async def run() -> None:
        try:
            await _base_due(session_factory, now)
        except Exception:  # noqa: BLE001 - фоновая задача не должна ронять планировщик
            log.exception("эталон проверки связи упал")

    _running = asyncio.create_task(run())
