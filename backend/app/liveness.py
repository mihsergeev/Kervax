"""Недоступен: молчит агент или сам сервер.

Алерт "недоступен" срабатывает, когда замолчал агент, и по нему не понять, что чинить: агент
(упал, не может достучаться до панели) или сам сервер (лежит, нет сети). В момент алерта
панель стучится к серверу сама:
- порт открыт - машина жива, молчит агент или его связь с панелью;
- отказ (RST) на порту, который был открыт, - адрес отвечает, машина в сети, но сервис на порту
  не работает: похоже на перезагрузку;
- ни ответа, ни отказа - лежит сервер или сеть до него. Если порты закрыты для панели
  файрволом, такая проверка ничего не доказывает, это сказано в тексте.

Куда стучаться: порты, открытые снаружи по последней проверке (app/open_ports.py), на адресах,
где отвечает именно эта нода (адрес шлюза NAT с чужим SSH отброшен, open_ports.foreign), плюс SSH
на собственных публичных адресах ноды и на адресе, заданном вручную (agent_ip). По частному
адресу у панели может отвечать другая машина, поэтому у SSH сверяется версия OpenSSH в баннере
с той, что у sshd ноды."""

from __future__ import annotations

import asyncio

from app import open_ports

_MAX = 6


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


async def check(srv) -> dict | None:
    """{state: alive|refused|silent, where: "адрес:порт", tried: [...]} или None, если стучаться
    некуда (агент не прислал ни адресов, ни портов)."""
    pairs = candidates(srv)
    if not pairs:
        return None
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
    return {"state": "silent", "where": "", "tried": tried}


def text(res: dict) -> str:
    st = res.get("state")
    if st == "alive":
        return (f"с панели сервер отвечает ({res['where']}): машина жива, молчит агент или его связь с "
                "панелью - systemctl status kervax-agent")
    if st == "refused":
        return (f"адрес отвечает отказом ({res['where']}): машина в сети, но порт не слушается - похоже "
                "на перезагрузку или упавший сервис")
    tried = ", ".join(res.get("tried") or [])
    return (f"с панели сервер не отвечает ({tried}): лежит сам сервер или сеть до него. Если эти порты "
            "закрыты для панели файрволом, проверка ничего не говорит")
