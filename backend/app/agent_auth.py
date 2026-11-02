"""Проверка источника уже авторизованного агента, без доверия его отчету.

Адреса прокси задает хостовый sync. Frontend имеет право передать XFF, а
forwarded_proxy_ips описывает только промежуточные узлы внутри этой цепочки.
"""
import ipaddress
import json
import math
from pathlib import Path
import time

from fastapi import HTTPException, Request

from app import gre
from app.config import get_settings
from app.models import Server

PROXY_FILE = "agent_proxy_peers.json"
PROXY_MAX_AGE = 180
PROXY_FUTURE_SKEW = 30
_MAX_FILE = 65536
_DENIED = "Источник запроса агента не разрешен"


def _ip(value) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        parsed = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
        parsed = parsed.ipv4_mapped
    if parsed.is_unspecified or parsed.is_multicast:
        return None
    return str(parsed)


def _proxy_peers(data_dir: str) -> tuple[set[str], set[str]] | None:
    """Отсутствующий/старый/неполный снимок не дает прав доверять заголовкам."""
    try:
        with (Path(data_dir) / PROXY_FILE).open("rb") as handle:
            raw = handle.read(_MAX_FILE + 1)
        if len(raw) > _MAX_FILE:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1:
            return None
        generated = value.get("generated_at")
        if type(generated) not in (int, float) or not math.isfinite(generated):
            return None
        age = time.time() - generated
        if age > PROXY_MAX_AGE or age < -PROXY_FUTURE_SKEW:
            return None
        roles = []
        for key in ("frontend_peer_ips", "forwarded_proxy_ips"):
            ips = value.get(key)
            if not isinstance(ips, list) or len(ips) > 128:
                return None
            canonical = {_ip(ip) for ip in ips}
            if None in canonical:
                return None
            roles.append(canonical)
        if roles[0] & roles[1]:
            return None
        return roles[0], roles[1]
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def source_ip(request: Request, data_dir: str) -> str | None:
    """Первый недоверенный узел справа. Заголовок от постороннего peer игнорируется."""
    peer = _ip(request.client.host if request.client else None)
    if peer is None:
        return None
    peers = _proxy_peers(data_dir)
    if peers is None:
        return None
    frontends, forwarded = peers
    if peer not in frontends:
        return peer
    values = request.headers.getlist("x-forwarded-for")
    if len(values) != 1 or len(values[0]) > 8192:
        return None
    chain = [_ip(part) for part in values[0].split(",")]
    if not chain or len(chain) > 64 or None in chain:
        return None
    for address in reversed(chain):
        if address not in forwarded:
            return address
    return None


def _configured_ips(server: Server) -> set[str]:
    primary = (server.agent_ip or "").strip()
    extras = getattr(server, "agent_source_ips", None)
    if extras is None:
        extras = []
    if not isinstance(extras, list) or len(extras) > 16:
        raise HTTPException(403, _DENIED)
    values = ([primary] if primary else []) + extras
    ips = {_ip(value) for value in values}
    if None in ips:
        raise HTTPException(403, _DENIED)
    return ips


async def require_agent_source(server: Server, request: Request, session) -> str | None:
    """Вызывать после проверки токена, до изменения БД или выдачи очереди команд.

    None означает прежнюю политику: guard выключен или адреса не заданы.
    Отчет агента, external_ip и gre_peer никогда не расширяют разрешения.
    """
    settings = get_settings()
    if not getattr(settings, "agent_source_guard", False):
        return None
    allowed = _configured_ips(server)
    if not allowed:
        return None
    source = source_ip(request, settings.data_dir)
    if source is None:
        raise HTTPException(403, _DENIED)
    if source in allowed:
        return source
    if server.gre_enabled:
        node = gre.addrs(server.gre_net or "")[1]
        if node and source == _ip(node):
            return source
    if server.relay_via_id is not None and server.relay_via_id != server.id:
        relay = await session.get(Server, server.relay_via_id)
        if relay is not None and relay.enabled and relay.relay_enabled:
            if source in _configured_ips(relay):
                return source
    raise HTTPException(403, _DENIED)
