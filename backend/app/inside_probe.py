"""Сайт не открывается снаружи - а изнутри его сервера?

Монитор из панели видит только "не открывается": таймаут, обрыв, отказ. Почему - вопрос
следующий, и первым делом его задают так: сайт жив, а мешает сеть по дороге (провайдер,
блокировка, белый список, файрвол), или лежит само приложение? Раньше за ответом шли на
сервер руками. Теперь при первом сбое монитора панель просит агента сервера, на котором
живет сайт, открыть его через localhost (тем же заданием, что у кнопки "Проверить сейчас"
для сайтов за белым списком). Порог алерта обычно две неудачи подряд, так что к моменту
алерта ответ изнутри уже есть и уходит строкой в том же сообщении - без отдельного письма.

Сервер берется только по однозначным признакам: его веб-сервер обслуживает этот домен
или цель монитора - его адрес. Родитель, заданный руками (шлюз, гипервизор), сайт может
и не держать: проверка изнутри него ничего бы не сказала.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

from app import checks as checks_exec
from app import parents as parents_mod
from app.models import Check, ProbeRequest, Server

KIND = "inside"
# не чаще одного запроса на монитор: сайт мигает - агента не дергаем каждую минуту
REPEAT = timedelta(minutes=5)
# сколько ответ изнутри годится для алерта
FRESH = timedelta(minutes=10)


def _port(target: str) -> int | None:
    t = (target or "").strip()
    try:
        return urlparse(t if "://" in t else "http://" + t).port
    except ValueError:
        return -1


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) and v else None
    except ValueError:
        return None


async def request(session: AsyncSession, check: Check, now: datetime) -> bool:
    """Первый сбой монитора из панели: задание агенту сервера сайта. True - задание ушло."""
    from app.collector import seen_online  # collector импортирует этот модуль
    from app.manual_probe import fast_agent

    if check.type != "http" or check.probe_local:
        return False
    if _port(check.target) not in (None, 80, 443):
        # монитор из панели на нестандартный порт завел кто угодно, а изнутри это был бы запрос
        # к localhost-сервису ноды (прокси docker-сокета, админки): туда не спрашиваем
        return False
    li = check.last_inside if isinstance(check.last_inside, dict) else {}
    asked = _ts(li.get("asked"))
    if asked is not None and now - asked < REPEAT:
        return False
    idx = parents_mod.Index(await parents_mod.load_servers(session))
    sid = next((pid for pid, why in parents_mod.parents_of(check, idx)
                if why in (parents_mod.WHY_SITE, parents_mod.WHY_IP)), None)
    srv = await session.get(Server, sid) if sid else None
    if srv is None or not srv.enabled or not fast_agent(srv) or not seen_online(srv, now):
        return False
    session.add(ProbeRequest(check_id=check.id, server_id=srv.id, url="", kind=KIND, created_at=now))
    srv.probe_pending_at = now
    check.last_inside = {"asked": now.isoformat(), "server_id": srv.id, "server": srv.name}
    return True


def store(check: Check, req: ProbeRequest, server_id: int, now: datetime) -> None:
    """Ответ агента: вердикт тем же разбором, что у локальных мониторов, - в монитор."""
    out = checks_exec.outcome_from_agent(check, req, now, check.degraded_ms)
    req.status = out.status
    req.message = out.message[:512]
    li = dict(check.last_inside) if isinstance(check.last_inside, dict) else {}
    li.update({"ts": now.isoformat(), "server_id": server_id, "status": out.status,
               "latency_ms": out.latency_ms, "message": out.message[:300]})
    check.last_inside = li


def alert_line(li, now: datetime) -> str:
    """Строка в алерт про сбой: что видно изнутри сервера. Пусто - ответа нет или он старый."""
    if not isinstance(li, dict):
        return ""
    ts = _ts(li.get("ts"))
    if ts is None or now - ts > FRESH or not li.get("status"):
        return ""
    srv = li.get("server") or "сервера"
    msg = li.get("message") or ""
    if li["status"] in ("up", "degraded"):
        return (f"изнутри {srv} открывается ({msg}): приложение живо, снаружи мешает сеть, "
                "блокировка или белый список")
    return f"изнутри {srv} тоже не открывается: {msg}"
