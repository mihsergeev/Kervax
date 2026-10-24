"""Приём запросов со сжатым телом (Content-Encoding: gzip).

Зачем. У части провайдеров (наблюдалось на РФ-нодах за DPI) TCP-соединение
убивается по счётчику ОТДАННЫХ вверх байт — примерно после 16–24 КБ. Отчёт
агента в 13 КБ уходит с первого раза, а второй по тому же соединению уже
попадает в чёрную дыру: запрос уходит, ответ не возвращается никогда. Сжатый
отчёт весит в 5–6 раз меньше и до порога не добирается даже на самых жирных
нодах парка. Агент шлёт gzip начиная с 1.90; несжатое тело принимаем как
раньше, поэтому старые агенты продолжают работать без изменений.

Только для /api/agent/* — снаружи распаковывать чужие тела незачем.
"""

import zlib

from sqlalchemy import select
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Потолок распакованного тела: отчёт самой крупной ноды парка — сотня килобайт,
# 8 МБ хватает с многократным запасом и не даёт разложить панель zip-бомбой.
_MAX_DECOMPRESSED = 8 * 1024 * 1024
# Сжатое тело больше этого не ждем вовсе: агент жмет в 5-6 раз, нормальный отчет - десятки КБ
_MAX_COMPRESSED = 2 * 1024 * 1024
_PREFIX = "/api/agent/"


async def _token_known(scope: Scope, headers: Headers) -> bool:
    """Тело распаковываем только для известного токена сервера. Раньше распаковка шла до
    проверки токена, и любой из интернета клал бэкенд gzip-бомбой. Сами эндпоинты токен
    проверяют еще раз: здесь только отсечь чужих до траты памяти."""
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return False
    app = scope.get("app")
    factory = getattr(getattr(app, "state", None), "session_factory", None)
    if factory is None:
        return False
    from app.models import Server
    from app.security import hash_agent_token

    async with factory() as session:
        sid = await session.scalar(
            select(Server.id).where(Server.token_hash == hash_agent_token(token)).limit(1))
    return sid is not None


def _gunzip(raw: bytes) -> bytes | None:
    """Распаковка с потолком: None - распакованное больше _MAX_DECOMPRESSED. Битое тело
    отдаем как есть - пусть валидатор ответит 422, а не 500."""
    d = zlib.decompressobj(wbits=31)
    try:
        out = d.decompress(raw, _MAX_DECOMPRESSED + 1)
        if len(out) > _MAX_DECOMPRESSED or d.unconsumed_tail:
            return None
        out += d.flush()
    except zlib.error:
        return raw
    if len(out) > _MAX_DECOMPRESSED:
        return None
    if not d.eof:
        return raw
    return out


class GzipRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(_PREFIX):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("content-encoding", "").lower() != "gzip":
            return await self.app(scope, receive, send)

        if not await _token_known(scope, headers):
            # тело не читаем вовсе: тот же ответ, что дал бы эндпоинт на чужой токен
            return await JSONResponse({"detail": "Неверный токен агента"}, status_code=401)(scope, receive, send)

        raw = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            raw += message.get("body", b"")
            if len(raw) > _MAX_COMPRESSED:
                return await JSONResponse({"detail": "Слишком большое тело"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break

        body = _gunzip(raw)
        if body is None:
            return await JSONResponse({"detail": "Слишком большое тело"}, status_code=413)(scope, receive, send)
        if body is not raw:
            # Агент кладет файлы helper'ов в отчет как есть, и один битый байт UTF-8 (helper обрезал
            # пример строки лога посреди русской буквы) ронял разбор всего тела: FastAPI отвечал 400,
            # отчет терялся целиком, и живая нода выглядела недоступной. Битые байты - в U+FFFD,
            # остальной отчет доходит
            try:
                body.decode("utf-8")
            except UnicodeDecodeError:
                body = body.decode("utf-8", "replace").encode("utf-8")

        # Content-Length должен описывать РАСПАКОВАННОЕ тело, иначе фреймворк
        # обрежет его на исходной длине; заголовок кодировки снимаем — тело уже сырое.
        scope = dict(scope)
        mutable = MutableHeaders(raw=list(scope["headers"]))
        del mutable["content-encoding"]
        mutable["content-length"] = str(len(body))
        scope["headers"] = mutable.raw

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
