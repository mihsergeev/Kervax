"""Адрес того, кто пришел в панель, за своими прокси.

Панель стоит за своими прокси (caddy, за ним nginx фронтенда) в docker-сети, и адрес
соединения - это прокси: все входы в журнале выглядели одним частным адресом, а блокировка
после неудач запирала всех сразу, а не того, кто подбирает пароль. Каждый прокси дописывает в
X-Forwarded-For, от кого пришел запрос: справа налево пропускаем свои прокси, первый чужой
адрес - клиент. Заголовку верим, только когда соединение от своего прокси: снаружи его можно
подставить любой. Нужен входу (api/auth.py) и счетчику левых запросов (request_watch.py).
"""

import ipaddress

# адреса своих прокси: docker-сети, локальная сеть хоста
PROXY_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
]


def is_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in n for n in PROXY_NETS)


def client_key(host: str | None, xff: str) -> str:
    """host - адрес соединения, xff - заголовок X-Forwarded-For как пришел."""
    host = host or "unknown"
    try:
        if not is_proxy(ipaddress.ip_address(host)):
            return host
    except ValueError:
        return host
    chain = []
    for part in (xff or "").split(","):
        try:
            chain.append(ipaddress.ip_address(part.strip()))
        except ValueError:
            continue
    for ip in reversed(chain):
        if not is_proxy(ip):
            return str(ip)
    # все из локальной сети: вход изнутри, левый адрес - тот, кого увидел первый прокси
    return str(chain[0]) if chain else host
