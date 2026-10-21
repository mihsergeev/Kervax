"""Ход обновления парка из блока "Обновления" главной и баннера "Серверов".

Что запустили и когда - на панели, а не во вкладке браузера: прогресс переживает перезагрузку
страницы и один у всех, кто смотрит. Одна запись в app_settings, две независимые части:
агент (OTA через панель: версия, ноды, время запуска - от него считается "застряла") и helper'ы
(команду ansible панель только копирует, ждем, пока хосты перестанут требовать прогона).
Часть живет сутки: дальше отстающие ноды и так видны обычной строкой "Обновлений".
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store

KEY = "fleet_rollout"
TTL = timedelta(hours=24)


def _alive(part: dict | None, now: datetime) -> dict | None:
    if not isinstance(part, dict):
        return None
    try:
        at = datetime.fromtimestamp(float(part.get("at", 0)), timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return part if now - at <= TTL else None


async def load(session: AsyncSession) -> dict:
    """{"agent": {"v", "ids", "at"} | None, "helpers": {"hosts", "at"} | None}; просроченное
    и битое - None."""
    raw = await settings_store.get_raw(session, KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    now = datetime.now(timezone.utc)
    agent = _alive(data.get("agent"), now)
    if agent and not (isinstance(agent.get("v"), str) and isinstance(agent.get("ids"), list)):
        agent = None
    helpers = _alive(data.get("helpers"), now)
    if helpers and not isinstance(helpers.get("hosts"), list):
        helpers = None
    return {"agent": agent, "helpers": helpers}


async def save(session: AsyncSession, data: dict) -> None:
    await settings_store.set_raw(session, KEY, json.dumps(data) if data.get("agent") or data.get("helpers") else "")


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


async def track_agent(session: AsyncSession, version: str, ids: list[int]) -> None:
    """Запуск агента на ids. Та же версия - ноды добавляются к идущей раскатке (повтор на
    застрявших и "Обновить" второй учеткой не сбрасывают "обновились 10 из 12"), часы
    "застряла" идут заново. Новая версия - раскатка с нуля."""
    data = await load(session)
    cur = data["agent"]
    keep = [i for i in cur["ids"] if isinstance(i, int)] if cur and cur["v"] == version else []
    data["agent"] = {"v": version, "ids": sorted(set(keep) | set(ids)), "at": _ts()}
    await save(session, data)


async def track_helpers(session: AsyncSession, hosts: list[str]) -> None:
    data = await load(session)
    cur = data["helpers"]
    keep = [h for h in cur["hosts"] if isinstance(h, str)] if cur else []
    data["helpers"] = {"hosts": sorted(set(keep) | set(hosts)), "at": _ts()}
    await save(session, data)


async def untrack(session: AsyncSession, part: str, ids: set[int] | None = None,
                  hosts: set[str] | None = None) -> None:
    """Убрать из части свои ноды (учетка видит только свои группы и чужие не трогает);
    пусто - части больше нет."""
    data = await load(session)
    cur = data.get(part)
    if not cur:
        return
    if part == "agent":
        cur["ids"] = [i for i in cur["ids"] if i not in (ids or set())]
        if not cur["ids"]:
            data["agent"] = None
    else:
        cur["hosts"] = [h for h in cur["hosts"] if h not in (hosts or set())]
        if not cur["hosts"]:
            data["helpers"] = None
    await save(session, data)
