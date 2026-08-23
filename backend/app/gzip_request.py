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

import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Потолок распакованного тела: отчёт самой крупной ноды парка — сотня килобайт,
# 8 МБ хватает с многократным запасом и не даёт разложить панель zip-бомбой.
_MAX_DECOMPRESSED = 8 * 1024 * 1024
_PREFIX = "/api/agent/"


class GzipRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(_PREFIX):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        if headers.get("content-encoding", "").lower() != "gzip":
            return await self.app(scope, receive, send)

        raw = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            raw += message.get("body", b"")
            if not message.get("more_body", False):
                break

        try:
            body = gzip.decompress(raw)
        except (OSError, EOFError):
            # битое/недосжатое тело — пусть валидатор ответит 422, а не 500
            body = raw
        if len(body) > _MAX_DECOMPRESSED:
            body = b""

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
