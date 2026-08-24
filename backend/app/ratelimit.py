"""Лимитер попыток входа (защита от подбора пароля).

Счётчик лежит в базе, а не в памяти процесса. Раньше он был словарём в модуле, и
это было верно ровно до тех пор, пока панель работала одним процессом uvicorn.
В scale-режиме (`compose.scale.yml`) воркеров несколько, у каждого свой словарь —
порог умножался на их число: 10 попыток превращались в 30 при трёх воркерах и в
80 при восьми. Снаружи защита при этом выглядела рабочей: часть запросов честно
получала 429. Проверено на живой панели — из 45 попыток подбора 22 дошли до
проверки пароля.

Ключ — обычно IP клиента. Окно и порог подобраны консервативно; при превышении —
временная блокировка.
"""

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LoginFailure

WINDOW = 300  # сек: окно подсчёта неудачных попыток
MAX_FAILURES = 10  # неудач в окне до блокировки
LOCKOUT = 900  # сек: как долго храним попытки (после этого запись не нужна)


def _now(now: float | None) -> datetime:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)


async def _recent(session: AsyncSession, key: str, moment: datetime) -> int:
    return int(await session.scalar(
        select(func.count())
        .select_from(LoginFailure)
        .where(LoginFailure.key == key, LoginFailure.ts >= moment - timedelta(seconds=WINDOW))
    ) or 0)


async def is_locked(session: AsyncSession, key: str, *, now: float | None = None) -> bool:
    return await _recent(session, key, _now(now)) >= MAX_FAILURES


async def record_failure(
    session: AsyncSession, key: str, *, now: float | None = None
) -> bool:
    """Регистрирует неудачную попытку. Возвращает True, если ИМЕННО эта попытка
    перевела ключ в состояние блокировки (для однократного алерта)."""
    moment = _now(now)
    session.add(LoginFailure(key=key, ts=moment))
    # чистим то, что уже никогда не попадёт в окно, — таблица не растёт
    await session.execute(
        delete(LoginFailure).where(LoginFailure.ts < moment - timedelta(seconds=LOCKOUT))
    )
    await session.commit()
    return await _recent(session, key, moment) == MAX_FAILURES


async def clear(session: AsyncSession, key: str) -> None:
    await session.execute(delete(LoginFailure).where(LoginFailure.key == key))
    await session.commit()
