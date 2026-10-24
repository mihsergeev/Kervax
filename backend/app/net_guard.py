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


async def host_class(host: str, timeout: float = 3.0) -> str:
    """Худший класс адресов хоста. Не резолвится - судим по имени: снаружи такого все равно нет."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return PUBLIC
    if _is_ip(h):
        return ip_class(h)
    if h in _METADATA_NAMES:
        return FORBIDDEN
    if "." not in h or h.endswith((".local", ".internal", ".localhost")) or h == "localhost":
        cls = INTERNAL
    else:
        cls = PUBLIC
    for ip in await _resolve(h, timeout):
        c = ip_class(ip)
        if _RANK[c] > _RANK[cls]:
            cls = c
    return cls


def request_hook(proxy: bool = False):
    """event_hooks["request"] для httpx: первый запрос задает, внешний ли это адрес, каждый
    следующий (редирект) не может уйти внутрь, если начинали снаружи. Через прокси адрес
    резолвит прокси, и смотрим только на само имя/IP."""
    state: dict[str, str] = {}

    async def hook(request: httpx.Request) -> None:
        host = request.url.host
        # проверка "все адреса" идет на IP с заголовком Host: смотрим на IP
        cls = ip_class(host) if _is_ip(host) or proxy else await host_class(host, 2.0)
        if proxy and not _is_ip(host) and host.lower().rstrip(".") in _METADATA_NAMES:
            cls = FORBIDDEN
        first = state.setdefault("first", cls)
        if cls == FORBIDDEN:
            raise httpx.ConnectError(FORBIDDEN_TEXT, request=request)
        if cls == INTERNAL and first == PUBLIC:
            raise httpx.ConnectError(INTERNAL_TEXT, request=request)

    return hook


async def connect_ok(host: str) -> str:
    """Для tcp и сертификата: пусто - можно, иначе текст ошибки."""
    return FORBIDDEN_TEXT if await host_class(host, 2.0) == FORBIDDEN else ""
