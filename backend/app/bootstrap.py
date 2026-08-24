import logging

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import AppSetting, Location, User
from app.security import MIN_PASSWORD_LEN, hash_password

log = logging.getLogger("kervax.bootstrap")

_GEO_SEEDED_KEY = "default_location_seeded"


async def _panel_geo_name() -> str:
    """Имя локации по геолокации публичного IP панели (ip-api.com, RU-локализация)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "http://ip-api.com/json/?lang=ru&fields=status,country,city"
            )
            data = r.json()
        if data.get("status") == "success":
            name = f"{data.get('country', '')}, {data.get('city', '')}".strip(", ").strip()
            if name:
                return name[:64]
    except Exception:  # noqa: BLE001 — сеть недоступна → фолбэк
        log.warning("не удалось определить геолокацию панели", exc_info=True)
    return "Панель (напрямую)"


async def ensure_default_location(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """При первом старте заводит «прямую» локацию (без прокси) с именем по гео IP
    панели. Делается один раз (флаг в app_settings) — если удалить, не воскресает."""
    async with session_factory() as session:
        if await session.get(AppSetting, _GEO_SEEDED_KEY) is not None:
            return
        has_any = await session.scalar(select(func.count()).select_from(Location))
        if not has_any:
            name = await _panel_geo_name()
            session.add(Location(name=name, url="", enabled=True))
            log.info("создана локация по умолчанию (прямая): %s", name)
        session.add(AppSetting(key=_GEO_SEEDED_KEY, value="1"))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # другой воркер уже засеял (гонка N воркеров)


async def ensure_admin(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Заводит админа ТОЛЬКО при первом старте (когда пользователя ещё нет).

    Пароль НЕ пересинхронизируется с .env на каждом запуске — иначе смена пароля
    через UI откатывалась бы при рестарте. Пароль из .env — только начальный;
    дальше меняется через POST /auth/password.

    Break-glass: если KERVAX_ADMIN_PASSWORD_RESET=1 — сбрасывает пароль на
    admin_password и отключает 2FA (на случай утери пароля И 2FA).
    """
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.username == settings.admin_user)
        )
        if user is None:
            # Первый старт: пароль из .env станет настоящим паролем админа, и
            # проверить его длину больше негде — через API он уже не заводится.
            # На существующей установке эта ветка не выполняется, поэтому старый
            # короткий пароль в .env не мешает обновиться (он там уже мёртвый).
            if len(settings.admin_password) < MIN_PASSWORD_LEN:
                raise RuntimeError(
                    f"KERVAX_ADMIN_PASSWORD короче {MIN_PASSWORD_LEN} символов — "
                    "задайте длиннее. Панель не запущена в целях безопасности."
                )
            session.add(
                User(
                    username=settings.admin_user,
                    password_hash=hash_password(settings.admin_password),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()  # другой воркер уже создал админа
        elif settings.admin_password_reset:
            user.password_hash = hash_password(settings.admin_password)
            user.totp_enabled = False
            user.totp_secret = ""
            user.token_version += 1  # инвалидируем все старые токены
            await session.commit()
            log.warning(
                "АВАРИЙНЫЙ СБРОС пароля админа выполнен — удалите "
                "KERVAX_ADMIN_PASSWORD_RESET из .env и перезапустите"
            )
