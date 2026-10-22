"""Прогрев после включения: сервер только что загрузился или вернулся на связь.

Живой случай: ВМ выключили для переноса диска, через минуту после "снова на связи" пришли
"не отвечает" про сайты, которые агент проверяет изнутри, и еще через минуту отбои: поды
Kubernetes просто еще поднимались. Пока сервер молчал, монитор копил неудачи ("агент не
присылает результат"), алерт придерживал молчащий родитель (app/parents.py), и ушел он в ту же
минуту, как сервер вернулся, - с причиной из первого же отчета после загрузки.

Прогрев - WINDOW после загрузки (по аптайму из отчета) или после возврата на связь (BACK_KEY в
отчете, его ставит прием отчета, когда агент молчал столько, что пришел бы алерт "недоступен").
Пока Kubernetes еще поднимает поды, прогрев тянется до MAX. Все это время новые беды того, что
поднимается вместе с сервером (его сайты, юниты, контейнеры, поды, воркеры, очереди), молчат: не
теряются, а ждут. Не поднялось к концу прогрева - алерт, как обычно, один.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Сколько ждать после загрузки: docker и systemd поднимают свое за минуту-две, поды k8s-ноды
# за несколько минут, и на живом случае все встало за две минуты. Десять - с запасом
WINDOW = 600
# Kubernetes еще не поднял поды (тяжелые образы, база долго встает) - ждем дольше, но не вечно:
# под, который не встает, сам по себе беда
MAX = 1200
# в last_report: когда агент вернулся после долгого молчания
BACK_KEY = "back_at"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts(v) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None


def came_back(last_seen: datetime | None, now: datetime, silent_after: float,
              panel_started: datetime) -> bool:
    """Агент вернулся после молчания дольше silent_after (столько нужно для алерта
    "недоступен"). Молчание из-за самой панели (лежала, перезапускалась) не в счет: отчеты
    тогда не доходили у всех серверов разом, и прогрев после каждой выкладки глушил бы весь парк."""
    if last_seen is None:
        return False
    seen = _aware(last_seen)
    return seen >= panel_started and (now - seen).total_seconds() > silent_after


def keep_back(prev: str | None, now: datetime) -> str | None:
    """Отметку возврата переносим в следующий отчет, пока она еще может держать прогрев."""
    t = _ts(prev)
    return prev if t is not None and (now - t).total_seconds() < MAX else None


def started_at(rep: dict, last_seen: datetime | None) -> datetime | None:
    """С какого момента идет прогрев: загрузка или возврат на связь, что позже."""
    out: list[datetime] = []
    up = rep.get("uptime_seconds")
    if last_seen is not None and isinstance(up, (int, float)) and up > 0:
        out.append(_aware(last_seen) - timedelta(seconds=float(up)))
    back = _ts(rep.get(BACK_KEY))
    if back is not None:
        out.append(back)
    return max(out) if out else None


def starting(rep: dict) -> bool:
    """Kubernetes еще поднимает поды: под контроллера не работает или не готов."""
    from app.collector import kube_bad_pods  # collector импортирует этот модуль

    return bool(kube_bad_pods(rep))


def warming(rep: dict | None, last_seen: datetime | None, now: datetime) -> bool:
    """Сервер прогревается: его новые беды пока не алертим."""
    rep = rep if isinstance(rep, dict) else {}
    t = started_at(rep, last_seen)
    if t is None:
        return False
    age = (now - t).total_seconds()
    return age < WINDOW or (age < MAX and starting(rep))


def server_warming(s, now: datetime) -> bool:
    return warming(getattr(s, "last_report", None), getattr(s, "last_seen", None), now)


async def warming_ids(session, servers, now: datetime) -> set[int]:
    """Прогревающиеся серверы из parents.load_servers: их сайты пока не алертят. Поды для
    продления до MAX читаем только у тех, чье окно уже вышло: отчеты целиком ради этого не тянем."""
    from sqlalchemy import select

    from app.models import Server

    out: set[int] = set()
    later: list[int] = []
    for s in servers:
        if not getattr(s, "enabled", True):
            continue
        t = started_at(getattr(s, "last_report", None) or {}, getattr(s, "last_seen", None))
        if t is None:
            continue
        age = (now - t).total_seconds()
        if age < WINDOW:
            out.add(s.id)
        elif age < MAX:
            later.append(s.id)
    if later:
        rows = await session.execute(select(Server.id, Server.last_report["kube"]).where(Server.id.in_(later)))
        out |= {i for i, kube in rows.all() if isinstance(kube, dict) and starting({"kube": kube})}
    return out
