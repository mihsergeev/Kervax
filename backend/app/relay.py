"""Ретранслятор для агентов: прокси на одном из наших серверов до панели.

Агент, чей адрес режут на пути к панели (IP в списке блокировок у провайдера панели), ходит в
нее через прокси с логином (агент 2.44, proxy= в конфиге). Ретранслятор - такой прокси на сервере,
который до панели достает: его ставит helper relay-setup, когда в настройках сервера включена
галочка. Helper раз в минуту сам спрашивает панель (POST /api/agent/relay, токеном агента этой
ноды), что держать: порт, логин, пароль и адреса серверов, которые выбрали этот ретранслятор.
Пускает он ТОЛЬКО до панели (адрес берет из конфига своего агента, не из ответа панели) и
только с этих адресов; на них же открывает порт в ufw/firewalld. Выключили галочку - helper
снимает прокси, правила фаервола и сам себя.

Логин и пароль прокси - секрет, как адрес локации: в ответе списка серверов их нет, они уходят
только helper'у ретранслятора и в команду установки агента.
"""

from __future__ import annotations

import ipaddress
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Server

HELPER = "relay-setup"
DEFAULT_PORT = 8790
USER = "kervax"
# helper спрашивает панель раз в минуту: дольше этого молчит - не работает или не достает
STALE = timedelta(minutes=5)


def port_of(relay: Server) -> int:
    return relay.relay_port or DEFAULT_PORT


def address_of(server: Server) -> str:
    """Внешний адрес сервера: тот, что задан руками, иначе тот, с которого пришел агент."""
    for ip in ((server.agent_ip or "").strip(), (server.external_ip or "").strip()):
        if valid_ip(ip):
            return ip
    return ""


def valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    return True


def ensure_credentials(relay: Server) -> None:
    """Логин и пароль заводятся один раз и при выключении остаются: включили снова - агенты,
    поставленные через этот ретранслятор раньше, работают без переустановки."""
    if not relay.relay_user:
        relay.relay_user = USER
    if not relay.relay_password:
        relay.relay_password = secrets.token_urlsafe(18)  # без символов, которые надо кодировать в URL


def proxy_url(relay: Server) -> str:
    """Адрес прокси для proxy= агента: http://логин:пароль@адрес-ретранслятора:порт."""
    host = address_of(relay)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{relay.relay_user}:{relay.relay_password}@{host}:{port_of(relay)}"


def enable_problem(relay: Server, online: bool) -> str:
    """Почему этот сервер не годится в ретрансляторы, '' - годится.

    Ретранслятор сам ходит в панель напрямую: если его агент не на связи или сам ходит через
    прокси, пускать через него других бессмысленно - до панели не дойдут и они."""
    # агент за прокси CONNECT-ом ходит только к https; адрес панели не задан - судить не по чему
    if get_settings().panel_url.lower().startswith("http://"):
        return "Ретранслятор работает только с панелью по https"
    if relay.last_seen is None:
        return "Агент сервера еще не выходил на связь: ретранслятор должен сам доставать до панели"
    if not online:
        return "Сервер не на связи: ретранслятор должен сам доставать до панели"
    if (relay.last_report or {}).get("proxy"):
        return "Агент этого сервера сам ходит в панель через прокси: напрямую сервер до панели не достает"
    if not address_of(relay):
        return "Не известен внешний адрес сервера: укажите IP сервера в настройках"
    return ""


async def clients(session: AsyncSession, relay: Server) -> list[str]:
    """Адреса серверов, выбравших этот ретранслятор, - их helper пускает на порт прокси."""
    rows = await session.scalars(
        select(Server).where(Server.relay_via_id == relay.id, Server.id != relay.id))
    return sorted({ip for s in rows if (ip := address_of(s))})


def _count(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def view(relay: Server, now: datetime) -> dict | None:
    """Состояние ретранслятора для карточки. Коды, а не фразы: подписи делает интерфейс.

    state: off - выключен; no_helper - включен, helper еще не поставлен (он в "Требует
    действий"); waiting - helper стоит, но панель еще не спрашивал; ok - слушает; error -
    helper сообщил ошибку (error); stale - helper давно не спрашивал панель."""
    if not relay.relay_enabled:
        return None
    st = relay.relay_state or {}
    installed = ((relay.last_report or {}).get("setup_versions") or {}).get(HELPER)
    seen = relay.relay_seen_at
    if seen is not None and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    if seen is None:
        state = "waiting" if installed else "no_helper"
    elif now - seen > STALE:
        state = "stale"
    elif st.get("error"):
        state = "error"
    else:
        state = "ok"
    return {
        "state": state,
        "port": port_of(relay),
        "clients": _count(st.get("clients")),
        "firewall": str(st.get("firewall") or ""),
        "error": str(st.get("error") or "")[:300],
        "conns": int(st.get("conns") or 0),
        "denied": int(st.get("denied") or 0),
        "seen_at": seen.isoformat() if seen else None,
        "helper": str(installed) if installed else "",
    }


def helper_status(body: dict) -> dict:
    """Что helper сообщил о себе - только известные поля и в разумных размерах."""
    fw = str(body.get("firewall") or "")
    return {
        "version": str(body.get("version") or "")[:16],
        "listening": bool(body.get("listening")),
        "firewall": fw if fw in ("ufw", "firewalld", "none") else "",
        "error": str(body.get("error") or "")[:300],
        "conns": _count(body.get("conns")),
        "denied": _count(body.get("denied")),
        "clients": _count(body.get("clients")),
    }
