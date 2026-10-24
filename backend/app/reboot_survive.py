"""Не переживет перезагрузку: сейчас работает, а после ребута не поднимется.

Контейнер без restart policy (no, пусто) или с on-failure: докер после перезапуска демона
поднимает только always и unless-stopped. На одной ноде cadvisor без restart policy работал два
года - просто ни разу не перезагружались. Лечится одной командой:
docker update --restart unless-stopped <имя>.

Сервис systemd, запущенный systemctl start без enable, когда его не тянет ни сокет, ни таймер,
ни D-Bus, ни другой юнит (helper units-setup 0.3): после ребута его нет. Лечится
systemctl enable <юнит>.

Контейнер, которого запускает юнит systemd (ExecStart=docker run --rm --name ..., Restart=always),
после ребута поднимет systemd, restart no у него - не ошибка. Юнит находит агент: включенный
юнит с docker run --name <имя> в ExecStart, процесс docker run в cgroup юнита или юнит с именем
контейнера ("unit", "unit_on"). Включенный юнит - контейнер не считаем. Не включенный - это
пункт про юнит (systemctl enable), а не про контейнер; helper units-setup, если он стоит,
решает про юниты точнее (видит, кого тянут сокет, таймер и другие юниты). docker run --rm
(auto_rm) без найденного юнита - считаем, но docker update --restart не предлагаем: на --rm
докер его не примет, а запускает такой контейнер кто-то другой.

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


def _no_policy(rep: dict) -> list[dict]:
    """Работающие сутки и больше контейнеры без restart policy, кроме swarm, k8s и Coolify"""
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
        out.append(c)
    return out


def containers(rep: dict) -> list[dict]:
    """Работающие контейнеры, которые докер после ребута не поднимет и не поднимет юнит systemd:
    [{"name", "image", "policy", "autorm"}]. autorm - docker run --rm: docker update ему не поможет."""
    out = []
    for c in _no_policy(rep):
        if c.get("unit"):
            continue  # его запускает юнит: включенный поднимет, не включенный - пункт в units()
        out.append({"name": str(c["name"]), "image": str(c.get("image") or ""),
                    "policy": str(c.get("policy") or "") or "no", "autorm": bool(c.get("auto_rm"))})
    return out


def _helper_units(rep: dict, now: datetime) -> list[dict] | None:
    """Список helper units-setup; None - helper не стоит или давно молчит"""
    ub = ((rep or {}).get("extras") or {}).get("units")
    if not isinstance(ub, dict) or ub.get("v") != 1:
        return None
    ref = float((rep or {}).get("clock_unix") or 0) or now.timestamp()
    if ref - float(ub.get("ts") or 0) > _STALE_BLOCK:
        return None  # helper давно не отчитывался: список устарел
    return [u for u in ub.get("noboot") or [] if isinstance(u, dict)]


def units(rep: dict, now: datetime) -> list[dict]:
    """Сервисы, которые работают, но после ребута не запустятся: [{"unit", "started"}], у юнита,
    найденного по контейнеру, еще "container"."""
    hl = _helper_units(rep, now)
    out = []
    for u in hl or []:
        if not u.get("unit") or _ON_DEMAND_UNIT.match(str(u["unit"])):
            continue
        started = int(u.get("started") or 0)
        if started and now.timestamp() - started < _DAY:
            continue
        out.append({"unit": str(u["unit"]), "started": started})
    if hl is None:
        # без helper'а про юнит знает только агент: юнит контейнера не включен
        seen = set()
        for c in _no_policy(rep):
            unit = str(c.get("unit") or "")
            if unit and not c.get("unit_on") and unit not in seen:
                seen.add(unit)
                out.append({"unit": unit, "started": 0, "container": str(c["name"])})
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
