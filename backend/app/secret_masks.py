"""Маскирование адресов мониторов в ответах для учетки только для просмотра."""

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit


_SECRET_PARAM = re.compile(r"(?i)(token|key|secret|pass|pwd|auth|sig|signature|session|sid|code|hash|jwt)")


def mask_target(target: str) -> str:
    """Прячет user:pass@ и значения секретных query-параметров, включая кодированные имена."""
    t = target or ""
    try:
        parts = urlsplit(t if "://" in t else "//" + t)
    except ValueError:
        # Неразбираемый адрес не должен возвращать пароль лишь из-за ошибки URL-парсера.
        return "***"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        query = "&".join(
            (k + "=***") if (sep and _SECRET_PARAM.search(unquote_plus(k))) else (k + sep + v)
            for k, sep, v in (x.partition("=") for x in query.split("&")))
    if (netloc, query) == (parts.netloc, parts.query):
        return t
    out = urlunsplit(parts._replace(netloc=netloc, query=query))
    return out if "://" in t else out.removeprefix("//")
