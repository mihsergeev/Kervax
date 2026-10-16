"""Защита входа от подбора пароля: счетчик неудач, блокировка адреса и белый список.

Счётчик лежит в базе, а не в памяти процесса. Раньше он был словарём в модуле, и
это было верно ровно до тех пор, пока панель работала одним процессом uvicorn.
В scale-режиме (`compose.scale.yml`) воркеров несколько, у каждого свой словарь —
порог умножался на их число: 10 попыток превращались в 30 при трёх воркерах и в
80 при восьми. Снаружи защита при этом выглядела рабочей: часть запросов честно
получала 429. Проверено на живой панели — из 45 попыток подбора 22 дошли до
проверки пароля.

Ключ — обычно IP клиента. До 1.4.237 после 10 неудач за 5 минут вход закрывался, пока
старые попытки не выйдут из окна, - на те же минут пять, и подбор шел дальше со скоростью
10 паролей за 5 минут. Теперь после max_failures неудач за WINDOW адрес блокируется на
ban_minutes (LoginBan), повторно в течение суток - на сутки. Адреса из белого списка не
блокируются никогда и их неудачи не считаются: свой офис или VPN не запрет ни опечатка, ни
чужой подбор с соседнего адреса. Пароль и код с них все равно спрашиваются.
"""

import ipaddress
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.models import LoginBan, LoginFailure

WINDOW = 900  # сек: окно подсчёта неудачных попыток
MAX_FAILURES = 5  # неудач в окне до блокировки, по умолчанию
BAN_MINUTES = 60  # на сколько блокировать, по умолчанию
REPEAT_MINUTES = 24 * 60  # повторная блокировка за сутки - на сутки
FAIL_CHOICES = (3, 5, 10, 20)
BAN_CHOICES = (15, 60, 360, 1440)
ALLOW_MAX = 100
SETTING_KEY = "login_guard"


@dataclass
class Guard:
    max_failures: int = MAX_FAILURES
    ban_minutes: int = BAN_MINUTES
    allow: list[dict] = field(default_factory=list)  # [{"net": "203.0.113.0/24", "note": "офис"}]

    def allowed(self, key: str) -> dict | None:
        """Запись белого списка, в которую попадает адрес, или None."""
        try:
            ip = ipaddress.ip_address(key)
        except ValueError:
            return None
        for a in self.allow:
            try:
                if ip in ipaddress.ip_network(a["net"], strict=False):
                    return a
            except (ValueError, KeyError, TypeError):
                continue
        return None


def parse_net(raw: str) -> str:
    """Адрес или подсеть в каноническом виде (203.0.113.7, 203.0.113.0/24, 2001:db8::/48).
    Одиночный адрес - без /32. ValueError, если это не адрес."""
    net = ipaddress.ip_network(raw.strip(), strict=False)
    return str(net.network_address) if net.num_addresses == 1 else str(net)


async def load(session: AsyncSession) -> Guard:
    raw = await settings_store.get_raw(session, SETTING_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    g = Guard()
    if data.get("max_failures") in FAIL_CHOICES:
        g.max_failures = data["max_failures"]
    if data.get("ban_minutes") in BAN_CHOICES:
        g.ban_minutes = data["ban_minutes"]
    g.allow = [a for a in data.get("allow") or [] if isinstance(a, dict) and a.get("net")]
    return g


async def save(session: AsyncSession, g: Guard) -> None:
    await settings_store.set_raw(session, SETTING_KEY, json.dumps(
        {"max_failures": g.max_failures, "ban_minutes": g.ban_minutes, "allow": g.allow},
        ensure_ascii=False,
    ))


def _now(now: float | None) -> datetime:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)


async def _recent(session: AsyncSession, key: str, moment: datetime) -> int:
    return int(await session.scalar(
        select(func.count())
        .select_from(LoginFailure)
        .where(LoginFailure.key == key, LoginFailure.ts >= moment - timedelta(seconds=WINDOW))
    ) or 0)


async def active_ban(session: AsyncSession, key: str, *, now: float | None = None) -> LoginBan | None:
    return await session.scalar(
        select(LoginBan).where(LoginBan.key == key, LoginBan.until > _now(now))
        .order_by(LoginBan.id.desc()).limit(1)
    )


async def note_blocked(session: AsyncSession, ban: LoginBan) -> None:
    """Попытка во время блокировки: только счетчик у блокировки, без записи в журнал на
    каждую - бот, долбящий вход, иначе раздувает журнал сотнями тысяч строк."""
    ban.blocked = (ban.blocked or 0) + 1
    await session.commit()


async def record_failure(
    session: AsyncSession, key: str, guard: Guard | None = None, username: str = "",
    *, now: float | None = None,
) -> LoginBan | None:
    """Регистрирует неудачную попытку. Возвращает блокировку, если ИМЕННО эта попытка
    ее включила (для записи в журнал и алерта), иначе None."""
    g = guard or Guard()
    moment = _now(now)
    session.add(LoginFailure(key=key, ts=moment, username=username[:64]))
    # чистим то, что уже никогда не попадёт в окно, — таблицы не растут
    await session.execute(delete(LoginFailure).where(LoginFailure.ts < moment - timedelta(seconds=WINDOW)))
    await session.execute(delete(LoginBan).where(LoginBan.until < moment - timedelta(days=2)))
    await session.flush()
    n = await _recent(session, key, moment)
    ban = None
    if n >= g.max_failures and await active_ban(session, key, now=now) is None:
        prev = await session.scalar(
            select(LoginBan.id).where(LoginBan.key == key, LoginBan.until >= moment - timedelta(days=1)).limit(1)
        )
        names = sorted({u for u in await session.scalars(
            select(LoginFailure.username).where(LoginFailure.key == key)) if u})
        minutes = max(g.ban_minutes, REPEAT_MINUTES) if prev else g.ban_minutes
        ban = LoginBan(
            key=key, created_at=moment, until=moment + timedelta(minutes=minutes), failures=n,
            logins=", ".join(names)[:255], repeat=prev is not None,
        )
        session.add(ban)
        # после блокировки счет с нуля: когда она кончится, у адреса снова max_failures попыток
        await session.execute(delete(LoginFailure).where(LoginFailure.key == key))
    await session.commit()
    return ban


async def clear(session: AsyncSession, key: str) -> None:
    await session.execute(delete(LoginFailure).where(LoginFailure.key == key))
    await session.commit()


async def unban(session: AsyncSession, key: str) -> bool:
    """Снять блокировку вручную (окно Пользователи). Повторная блокировка после этого -
    снова обычная, не суточная: админ сам решил, что адрес свой."""
    n = (await session.execute(delete(LoginBan).where(LoginBan.key == key))).rowcount
    await session.execute(delete(LoginFailure).where(LoginFailure.key == key))
    await session.commit()
    return bool(n)
