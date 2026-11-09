"""Куда панель может ходить по просьбе учетки: мониторы, пробная проверка, признаки взлома.

Монитор - это запрос с хоста панели туда, куда сказали. Без ограничений это был сканер
внутренней сети docker и чтение метаданных облака (169.254.169.254, user-data бывает с
паролями): editor заводил монитор или жал "Проверить" и по коду, задержке и keyword-биту
узнавал, что там. Теперь:
- метаданные облака, link-local, 0.0.0.0 и multicast - никогда и никому, на каждом шаге
  редиректа;
- внутренние адреса (loopback, частные сети, имена без точки) - монитор на них заводит
  только админ, редирект с внешнего сайта во внутреннюю сеть не идет никогда.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

FORBIDDEN = "forbidden"
INTERNAL = "internal"
PUBLIC = "public"

# адреса метаданных облаков вне link-local: AWS по IPv6 и Alibaba
_METADATA = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
_METADATA_NAMES = {"metadata.google.internal", "metadata", "instance-data", "instance-data.ec2.internal"}

FORBIDDEN_TEXT = "адрес закрыт для проверок из панели (метаданные облака, link-local)"
INTERNAL_TEXT = "редирект во внутреннюю сеть не выполняется"
ADMIN_ONLY_TEXT = "Проверку внутреннего адреса (localhost, частные сети, имя без точки) задает только администратор"

_RANK = {PUBLIC: 0, INTERNAL: 1, FORBIDDEN: 2}


def ip_class(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip.strip("[]").split("%")[0])
    except ValueError:
        return PUBLIC
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr in _METADATA or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
        return FORBIDDEN
    if addr.is_loopback or addr.is_private or addr.is_reserved or not addr.is_global:
        return INTERNAL
    return PUBLIC


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%")[0])
        return True
    except ValueError:
        return False


async def _resolve(host: str, timeout: float) -> list[str]:
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        return []
    return [str(i[4][0]) for i in infos]


def cert_endpoint(target: str, port: int = 0) -> tuple[str, int]:
    """Parse certificate targets identically for validation and the TLS probe."""
    target = target.strip()
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in target):
        raise ValueError("invalid certificate target")
    try:
        ipaddress.ip_address(target)
    except ValueError:
        pass
    else:
        return target, port or 443
    url = urlsplit(target if "://" in target else "//" + target)
    host = url.hostname
    inline_port = url.port
    if not host or url.username is not None or url.password is not None or inline_port == 0:
        raise ValueError("invalid certificate target")
    if url.netloc.startswith("["):
        suffix = url.netloc.split("]", 1)[1]
        if suffix and not (suffix.startswith(":") and inline_port):
            raise ValueError("invalid certificate authority")
    return host, port or inline_port or 443


def address_class(host: str) -> str:
    if _is_ip(host):
        return ip_class(host)
    if host in _METADATA_NAMES:
        return FORBIDDEN
    if host and ("." not in host or host.endswith((".local", ".internal", ".localhost"))):
        return INTERNAL
    return PUBLIC


async def classified_addresses(host: str, timeout: float) -> tuple[str, list[str]]:
    """Return the worst class and the exact addresses checked for this lookup."""
    h = (host or "").strip().lower().rstrip(".")
    cls = address_class(h)
    if _is_ip(h):
        return cls, [h]
    if not h or cls == FORBIDDEN:
        return cls, []
    addresses = list(dict.fromkeys(await _resolve(h, timeout)))
    for ip in addresses:
        c = ip_class(ip)
        if _RANK[c] > _RANK[cls]:
            cls = c
    return cls, addresses


async def host_class(host: str, timeout: float = 3.0) -> str:
    cls, _ = await classified_addresses(host, timeout)
    return cls


def request_hook(proxy: bool = False, allow_internal: bool = True):
    """event_hooks["request"] для httpx: первый запрос задает, внешний ли это адрес, каждый
    следующий (редирект) не может уйти внутрь, если начинали снаружи. Через прокси адрес
    резолвит прокси, и смотрим только на само имя/IP."""
    state: dict[str, str] = {}

    async def hook(request: httpx.Request) -> None:
        host = request.url.host
        # проверка "все адреса" идет на IP с заголовком Host: смотрим на IP
        cls = address_class(host.lower().rstrip(".")) if proxy else await host_class(host, 2.0)
        first = state.setdefault("first", cls)
        if cls == FORBIDDEN:
            raise httpx.ConnectError(FORBIDDEN_TEXT, request=request)
        if cls == INTERNAL and (not allow_internal or first == PUBLIC):
            raise httpx.ConnectError(INTERNAL_TEXT if allow_internal else ADMIN_ONLY_TEXT, request=request)

    return hook


class MonitorTransport(httpx.AsyncBaseTransport):
    """Connect to the classified address while leaving redirects and auth to HTTPX."""

    def __init__(
        self, verify: bool, allow_internal: bool, timeout: float,
        address: str | None = None,
    ):
        # The pool sees numeric origins. Don't reuse TLS connections for another hostname.
        self.transport = httpx.AsyncHTTPTransport(
            verify=verify, limits=httpx.Limits(max_keepalive_connections=0),
        )
        self.allow_internal = allow_internal
        self.timeout = timeout
        self.first: str | None = None
        self.first_origin: tuple[str, str, int | None] | None = None
        self.address = address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        origin = (request.url.scheme, host, request.url.port)
        if self.first_origin is None:
            self.first_origin = origin
        if self.address and origin == self.first_origin:
            cls = address_class(host.lower().rstrip("."))
            address_cls = ip_class(self.address)
            if _RANK[address_cls] > _RANK[cls]:
                cls = address_cls
            addresses = [self.address]
        else:
            cls, addresses = await classified_addresses(host, min(2.0, self.timeout))
        if self.first is None:
            self.first = cls
        if cls == FORBIDDEN:
            raise httpx.ConnectError(FORBIDDEN_TEXT, request=request)
        if cls == INTERNAL and (not self.allow_internal or self.first == PUBLIC):
            message = INTERNAL_TEXT if self.allow_internal else ADMIN_ONLY_TEXT
            raise httpx.ConnectError(message, request=request)
        if not addresses:
            raise httpx.ConnectError("DNS lookup failed", request=request)
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = request.url.raw_host.decode("ascii").strip("[]")
        for address in addresses:
            pinned = httpx.Request(
                request.method, request.url.copy_with(host=address), headers=request.headers,
                stream=request.stream, extensions=extensions,
            )
            try:
                return await self.transport.handle_async_request(pinned)
            except httpx.ConnectError as exc:
                last_error = exc
        raise last_error

    async def aclose(self) -> None:
        await self.transport.aclose()


def block_reason(cls: str, allow_internal: bool) -> str:
    if cls == FORBIDDEN:
        return FORBIDDEN_TEXT
    if cls == INTERNAL and not allow_internal:
        return ADMIN_ONLY_TEXT
    return ""
