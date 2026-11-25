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
# Какой сервер и откуда пришел с верным токеном, но не с разрешенного адреса (app_settings):
# request_watch по этому называет сервер в алерте "левые запросы", а не "чужой адрес"
DENIED_KEY = "agent_denied"
_DENIED_EVERY = 60.0
_DENIED_KEEP = 86400
_denied_mem: dict[int, tuple[str, float]] = {}


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


def proxy_peers(data_dir: str) -> tuple[set[str], set[str]] | None:
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
    peers = proxy_peers(data_dir)
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


async def _remember_denied(session, server: Server, source: str) -> None:
    """Запомнить отказ своему агенту: сервер и адрес. Новый сервер за NAT, которому разрешили
    внутренний адрес, иначе выглядел в алерте как чужой адрес. Не чаще раза в минуту на сервер в
    процессе (агент стучится раз в несколько секунд); запись подсказки не меняет ответ агенту."""
    # set_raw коммитит сессию: с несохраненными изменениями запроса не пишем, чтобы не сохранить их
    if session is None or any(getattr(session, attr, None) for attr in ("new", "dirty", "deleted")):
        return
    now = time.time()
    last = _denied_mem.get(server.id)
    # раз в минуту на сервер, даже если адрес другой: с утекшим токеном адреса можно менять
    if last and now - last[1] < _DENIED_EVERY:
        return
    _denied_mem[server.id] = (source, now)
    from app import settings_store
    try:
        raw = await settings_store.get_raw(session, DENIED_KEY)
        data = json.loads(raw) if raw else {}
        data = {k: v for k, v in (data if isinstance(data, dict) else {}).items()
                if isinstance(v, dict) and isinstance(v.get("t"), (int, float)) and now - v["t"] < _DENIED_KEEP}
        data[str(server.id)] = {"ip": source, "t": int(now)}
        await settings_store.set_raw(session, DENIED_KEY, json.dumps(data, sort_keys=True))
    except Exception:  # noqa: BLE001
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


async def denied_sources(session, now: float, within: float) -> dict[str, list[int]]:
    """Адрес -> серверы, чьему агенту с этого адреса отказали за последние within секунд."""
    from app import settings_store
    try:
        data = json.loads(await settings_store.get_raw(session, DENIED_KEY) or "{}")
    except ValueError:
        return {}
    out: dict[str, list[int]] = {}
    for sid, v in (data if isinstance(data, dict) else {}).items():
        if (isinstance(v, dict) and isinstance(v.get("t"), (int, float)) and now - v["t"] < within
                and isinstance(v.get("ip"), str) and str(sid).isdigit()):
            out.setdefault(v["ip"], []).append(int(sid))
    return out


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
    await _remember_denied(session, server, source)
    raise HTTPException(403, _DENIED)
