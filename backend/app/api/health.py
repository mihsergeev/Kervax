import asyncio

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.config import get_settings

router = APIRouter(tags=["system"])

# Столько ждём ответа базы: health должен успеть ответить раньше, чем истечёт
# таймаут докеровского healthcheck (5 с) и curl в quickstart.
DB_TIMEOUT = 3.0


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    """Готовность панели: отвечает процесс И доступна база.

    Раньше здесь возвращалось безусловное «ok». При недоступной базе панель не
    могла ни впустить пользователя, ни собрать метрики — а health продолжал
    рапортовать, что всё хорошо. На этот ответ смотрят healthcheck контейнера
    (по нему в scale-режиме стартует scheduler), ожидание в quickstart и внешний
    мониторинг: зелёный статус при мёртвой базе — ровно то, за что панель
    мониторинга и ругает чужие проверки.
    """
    settings = get_settings()
    body = {"status": "ok", "version": settings.version, "db": "ok"}
    try:
        async with asyncio.timeout(DB_TIMEOUT):
            async with request.app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
    except Exception:
        body["status"] = "degraded"
        body["db"] = "down"
        response.status_code = 503
    return body
