"""Не переживет перезагрузку: сейчас работает, а после ребута не поднимется.

Контейнер без restart policy (no, пусто) или с on-failure: докер после перезапуска демона
поднимает только always и unless-stopped. На одной ноде cadvisor без restart policy работал два
года - просто ни разу не перезагружались. Лечится одной командой:
docker update --restart unless-stopped <имя>.

Сервис systemd, запущенный systemctl start без enable, когда его не тянет ни сокет, ни таймер,
ни D-Bus, ни другой юнит (helper units-setup 0.3): после ребута его нет. Лечится
systemctl enable <юнит>.

Не считаем: задачи swarm (имя.N.id, их перезапускает swarm), контейнеры Kubernetes (k8s_*),
контейнеры Coolify (его sentinel без restart policy поднимает сам Coolify), консоли getty на
терминалах (getty@tty6 systemd поднимает сам, когда на терминал переключились, enable им не нужен)
и то, что работает меньше суток: разовые задачи и только что запущенное. Точечный мут - "noboot:<имя>", весь пункт -
"noboot".
"""
from __future__ import annotations

import re
from datetime import datetime

from app.models import Server

MUTE_PREFIX = "noboot:"
MUTE_ALL = "noboot"
_DAY = 86400
_STALE_BLOCK = 3600  # helper units-setup пишет раз в минуту
_SWARM_TASK = re.compile(r"\.\d+\.[a-z0-9]{25}$")
_ON_DEMAND_UNIT = re.compile(r"^(getty|autovt|serial-getty|container-getty|console-getty)@")
_UP_DAYS = re.compile(r"^Up (?:About an? |\d+ )(day|week|month|year)s?\b")


def _up_a_day(status: str) -> bool:
    """'Up 11 days', 'Up 2 months (healthy)' - сутки и больше; 'Up 3 hours' - нет"""
    return bool(_UP_DAYS.match(status or ""))


def containers(rep: dict) -> list[dict]:
    """Работающие контейнеры, которые докер после ребута не поднимет: [{"name", "image", "policy"}]."""
    dk = (rep or {}).get("docker") or {}
    if not dk.get("access"):
        return []
    out = []
    for c in dk.get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        pol = str(c.get("policy") or "")
        if pol not in ("", "no") and not pol.startswith("on-failure"):
            continue
        name, image = str(c.get("name") or ""), str(c.get("image") or "")
        if not name or name.startswith("k8s_") or _SWARM_TASK.search(name) or "coollabsio/" in image:
            continue
        if not _up_a_day(str(c.get("status") or "")):
            continue
        out.append({"name": name, "image": image, "policy": pol or "no"})
    return out


def units(rep: dict, now: datetime) -> list[dict]:
    """Сервисы, которые работают, но после ребута не запустятся: [{"unit", "started"}]."""
    ub = ((rep or {}).get("extras") or {}).get("units")
    if not isinstance(ub, dict) or ub.get("v") != 1:
        return []
    ref = float((rep or {}).get("clock_unix") or 0) or now.timestamp()
    if ref - float(ub.get("ts") or 0) > _STALE_BLOCK:
        return []  # helper давно не отчитывался: список устарел
    out = []
    for u in ub.get("noboot") or []:
        if not isinstance(u, dict) or not u.get("unit") or _ON_DEMAND_UNIT.match(str(u["unit"])):
            continue
        started = int(u.get("started") or 0)
        if started and now.timestamp() - started < _DAY:
            continue
        out.append({"unit": str(u["unit"]), "started": started})
    return out


def view(srv: Server, now: datetime) -> dict | None:
    """Для раздела Docker и карточки: {"docker": [...], "units": [...]}, у каждого muted."""
    rep = srv.last_report or {}
    mutes = set(srv.alert_mutes or [])
    dc = [{**c, "muted": MUTE_PREFIX + c["name"] in mutes} for c in containers(rep)]
    un = [{**u, "muted": MUTE_PREFIX + u["unit"] in mutes} for u in units(rep, now)]
    return {"docker": dc, "units": un} if dc or un else None


def advice(srv: Server, now: datetime) -> dict | None:
    """Для "Требует действий": незаглушенные контейнеры и юниты."""
    if MUTE_ALL in set(srv.alert_mutes or []):
        return None
    v = view(srv, now)
    if not v:
        return None
    dc = [c["name"] for c in v["docker"] if not c["muted"]]
    un = [u["unit"] for u in v["units"] if not u["muted"]]
    return {"docker": dc, "units": un} if dc or un else None
