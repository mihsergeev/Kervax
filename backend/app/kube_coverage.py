"""Ноды кластера, за которыми не смотрит ни один агент панели.

Кластер панель видит через агента на control-plane ноде: список нод приходит в его отчете.
Воркеры при этом бывают вообще без агента, и тогда на них не видно ни дисков, ни SMART, ни
упавших юнитов, ни процессов. На k8s-a-prc так неделями крутились 49 зависших chrome и
жгли 45 ядер из 96, а панель видела только то, что отдает Kubernetes. Здесь такие ноды
находятся, чтобы панель подсветила их в "Требует действий".

Нода считается под присмотром, если в панели есть сервер с тем же именем (или hostname из
отчета агента, в том числе короткий, до первой точки) либо с тем же адресом. Смотрим ВСЕ
серверы панели, а не только видимые учетке: нода, которую смотрит сервер из чужой группы,
все равно под присмотром.
"""

import ipaddress
from collections.abc import Iterable

from app.models import Server


def _norm(name: object) -> str:
    return str(name or "").strip().lower().rstrip(".")


def _short(name: object) -> str:
    return _norm(name).split(".", 1)[0]


def _public(ip: str) -> bool:
    """Адрес из интернета: такой можно подставить в "IP сервера" при добавлении. Внутренний
    (192.168.x, 10.x) для списка разрешенных адресов панели бесполезен."""
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def build_index(servers: Iterable[Server]) -> tuple[set[str], set[str]]:
    """Имена и адреса всех серверов панели - один раз на весь список."""
    names: set[str] = set()
    ips: set[str] = set()
    for s in servers:
        for n in (s.name, (s.last_report or {}).get("hostname")):
            names.add(_norm(n))
            names.add(_short(n))
        for ip in (s.local_ip, s.external_ip, s.agent_ip):
            if ip and ip.strip():
                ips.add(ip.strip())
    names.discard("")
    return names, ips


def unmonitored(server: Server, index: tuple[set[str], set[str]]) -> list[dict]:
    """Ноды кластера этого сервера без агента панели, кроме тех, где агент не нужен."""
    kube = (server.last_report or {}).get("kube") or {}
    if not isinstance(kube, dict) or not kube.get("access"):
        return []
    names, ips = index
    ignored = {_norm(x) for x in (server.kube_node_ignored or [])}
    out: list[dict] = []
    for n in kube.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name") or "").strip()[:253]
        key = _norm(name)
        if not key or key in ignored:
            continue
        ip = str(n.get("ip") or "").strip()[:64]
        if key in names or _short(key) in names or (ip and ip in ips):
            continue
        out.append({"name": name, "ip": ip, "public": _public(ip)})
    return out
