import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.api import (
    alerts,
    audit,
    auth,
    backup as backup_api,
    branding,
    checks,
    health,
    locations,
    servers,
    settings as settings_api,
    users as users_api,
    vault as vault_api,
)
from app.bootstrap import ensure_admin, ensure_default_location
from app.collector import collector_loop
from app.config import get_settings
from app.db import create_engine_and_factory
from app.gzip_request import GzipRequestMiddleware

_WEAK_SECRETS = {"", "changeme", "dev-insecure-change-me"}
_WEAK_PASSWORDS = {"", "changeme", "admin"}


def _enforce_secrets(settings) -> None:
    """Отказываемся стартовать в проде с дефолтными/слабыми секретами."""
    if settings.debug:
        return
    if settings.jwt_secret in _WEAK_SECRETS or len(settings.jwt_secret) < 32:
        raise RuntimeError(
            "KERVAX_JWT_SECRET не задан или слишком слабый — задайте случайный "
            "секрет (openssl rand -hex 32). Панель не запущена в целях безопасности."
        )
    if settings.admin_password in _WEAK_PASSWORDS:
        raise RuntimeError(
            "KERVAX_ADMIN_PASSWORD не задан или дефолтный — задайте надёжный "
            "пароль. Панель не запущена в целях безопасности."
        )


def create_app() -> FastAPI:
    # httpx на INFO печатает URL каждого запроса, а токен Telegram-бота живёт ПРЯМО
    # В URL (/bot<token>/sendMessage) — иначе валидный секрет оседает в логах, куда
    # смотрит любой, у кого есть `docker logs`. Оставляем только warning+.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    engine, session_factory = create_engine_and_factory(settings.db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _enforce_secrets(settings)
        await ensure_admin(session_factory, settings)
        await ensure_default_location(session_factory, settings)
        # Планировщик крутим здесь только в одиночном режиме. При масштабировании
        # (несколько веб-воркеров) его выносят в отдельный процесс app.scheduler_run,
        # а веб-воркеры ставят KERVAX_RUN_SCHEDULER=0 — иначе он запустится N раз.
        task = (
            asyncio.create_task(collector_loop(session_factory, settings))
            if settings.run_scheduler
            else None
        )
        yield
        if task is not None:
            task.cancel()
        await engine.dispose()

    # доки/схему API отдаём только в debug — в проде не раскрываем поверхность API
    docs_url = "/api/docs" if settings.debug else None
    openapi_url = "/api/openapi.json" if settings.debug else None
    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        docs_url=docs_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory

    # агент 1.90+ шлёт отчёт сжатым — см. gzip_request (обход DPI по счётчику байт)
    app.add_middleware(GzipRequestMiddleware)

    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(users_api.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(checks.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.include_router(locations.router, prefix="/api")
    app.include_router(servers.router, prefix="/api")
    app.include_router(servers.agent_router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(backup_api.router, prefix="/api")
    app.include_router(vault_api.router, prefix="/api")
    # брендирование: GET публичный (экран входа рисуется до авторизации),
    # загрузка и удаление — админские (проверка внутри роутера)
    app.include_router(branding.router, prefix="/api")
    return app


app = create_app()
