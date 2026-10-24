"""Белые списки обходятся подменой CF-Connecting-IP (helper webserver-setup 0.27).

Сайт за Cloudflare берет адрес клиента из заголовка: CF-Connecting-IP или X-Forwarded-For. Если
веб-сервер верит заголовку от любого адреса, а 80 или 443 открыт и в обход Cloudflare, прямой заход
на адрес ноды с поддельным заголовком проходит allow/deny: на одной ноде ingress-nginx брал адрес из
CF-Connecting-IP при proxy-real-ip-cidr по умолчанию (0.0.0.0/0), и Grafana с Portainer за
whitelist-source-range открывались любому.

helper раз в 15 минут присылает report.d/web-realip.json: кому из веб-серверов верят (nginx на
хосте, в контейнерах и подах - ingress-nginx тоже, по его готовому nginx.conf; Caddy по живому
конфигу; Traefik по аргументам и файлу), какие порты они слушают и сколько сайтов с allow/deny.
Признак, когда сошлось:
- доверие без ограничения источника: сеть шире /8 (/16 у IPv6) - 0.0.0.0/0 и подобное,
  forwardedHeaders.insecure у Traefik, remote_ip forwarded у старого Caddy; у Caddy в контейнере
  еще и частные сети (private_ranges): IPv6 до него идет через docker-proxy с адреса шлюза моста;
- свежая внешняя проверка видит 80 или 443 открытым напрямую при сайтах через Cloudflare
  (open_ports.cf_open). Фильтр, который пускает на эти порты только Cloudflare (nftables, ufw,
  NetworkPolicy, iptables mangle), проверка видит закрытым портом - и признака нет. Правила хоста
  сами по себе ничего не решают: на Cilium с kube-proxy-replacement и на k3s с klipper (hostPort,
  DNAT мимо INPUT) ufw трафик к ingress не видит вовсе.
Есть allow/deny (у Traefik - ipAllowList с ipStrategy) - красное, "белые списки обходятся", с алертом.
Нет - желтое, "IP клиента можно подделать": поддельный адрес видят логи, баны fail2ban, лимиты
входа WordPress и само приложение. Это в "Что сломано" и в карточке, без сообщения в Telegram.

Реплики одного ворклоада (Deployment, DaemonSet, StatefulSet) - один веб-сервер: у ingress-nginx с
replicaCount 3 было три одинаковые строки и в "Что сломано", и в карточке, и в алерте. Склеиваем по
владельцу пода, рядом - сколько подов.

Человек может отметить "так задумано" (cfip:<веб-сервер>), признак уходит. Решение стоит на ворклоаде
и переживает пересоздание подов.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime

from app import open_ports, podnames, tg_fmt
from app.models import Server

STALE = 3600  # блок старше часа - helper не работает, не судим по старому
MUTE_PREFIX = "cfip:"
_NAMES = {"nginx": "nginx", "caddy": "Caddy", "traefik": "Traefik"}


def block(rep: dict, now: datetime) -> list[dict] | None:
    b = ((rep or {}).get("extras") or {}).get("web-realip")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("items"), list):
        return None
    try:
        if now.timestamp() - float(b.get("ts") or 0) > STALE:
            return None
    except (TypeError, ValueError):
        return None
    return [x for x in b["items"] if isinstance(x, dict)]


def _strs(v) -> list[str]:
    return [str(x) for x in v if str(x or "").strip()] if isinstance(v, list) else []


def _where(x: dict) -> str:
    return str(x.get("where") or "host")


def _pod(x: dict) -> tuple[str, str] | None:
    """("ns", "под") для веб-сервера в поде, иначе None."""
    w = _where(x)
    if not w.startswith("pod "):
        return None
    ns, _sep, name = w[4:].partition("/")
    return ns, name


def _workload(x: dict, ctrls: dict[str, str] | None = None) -> str:
    """Владелец пода: из ctrl агента ("deployment/ingress-nginx-controller"), иначе имя без суффиксов."""
    ns, name = _pod(x) or ("", "")
    return podnames.workload(name, (ctrls or {}).get(f"pod:{ns or '?'}/{name}", ""))


def label(x: dict) -> str:
    """Кто: ingress-nginx, nginx, Caddy, Traefik."""
    srv = str(x.get("server") or "")
    pod = _pod(x)
    if srv == "nginx" and pod and "ingress" in pod[1]:
        return "ingress-nginx"
    return _NAMES.get(srv, srv or "веб-сервер")


def where_text(x: dict, ctrls: dict[str, str] | None = None) -> str:
    """"на хосте", "в поде default/ingress-nginx-controller", "в контейнере caddy"."""
    w = _where(x)
    pod = _pod(x)
    if pod:
        return f"в поде {pod[0]}/{_workload(x, ctrls)}" if pod[0] else f"в поде {_workload(x, ctrls)}"
    return "на хосте" if w == "host" else f"в контейнере {w}"


def key(x: dict, ctrls: dict[str, str] | None = None) -> str:
    """Ключ веб-сервера для решения "так задумано": под - по ворклоаду, его имя меняется с выкладкой."""
    pod = _pod(x)
    w = f"pod {pod[0]}/{_workload(x, ctrls)}" if pod else _where(x)
    return f"{x.get('server') or '?'}:{w}"


def trust_any(x: dict) -> str:
    """Чему веб-сервер верит от любого адреса, словами для текста ("set_real_ip_from 0.0.0.0/0"),
    или "" - доверие ограничено (Cloudflare, частные сети, отдельные адреса)."""
    srv = str(x.get("server") or "")
    in_box = _where(x) != "host"
    priv = False
    for f in _strs(x.get("from")):
        if f == "*":
            return {"traefik": "forwardedHeaders.insecure", "caddy": "remote_ip forwarded"}.get(srv, "всем")
        try:
            net = ipaddress.ip_network(f, strict=False)
        except ValueError:
            continue
        if net.is_private or net.is_loopback:
            # частные сети и loopback из интернета не приходят; у Caddy в контейнере - приходят
            # через docker-proxy (IPv6 до него идет с адреса шлюза моста)
            priv = priv or (srv == "caddy" and in_box and not net.is_loopback)
            continue
        if net.prefixlen < (8 if net.version == 4 else 16):
            word = {"nginx": "set_real_ip_from", "caddy": "trusted_proxies", "traefik": "trustedIPs"}.get(srv)
            return f"{word} {f}" if word else f
    return "trusted_proxies private_ranges" if priv else ""


def host_ports(srv: Server, x: dict) -> set[int]:
    """Порты ноды, на которых снаружи отвечает этот веб-сервер. Контейнер Docker - опубликованные
    порты на порты, что он слушает внутри; хост и под - как есть (балансировщик кластера отдает
    80 и 443 на те же порты пода). Не знаем портов - 80 и 443."""
    ports = {int(p) for p in x.get("ports") or [] if isinstance(p, int) or str(p).isdigit()}
    w = _where(x)
    if w != "host" and not w.startswith("pod "):
        for c in (((srv.last_report or {}).get("docker") or {}).get("containers") or []):
            if not isinstance(c, dict) or str(c.get("name") or "").lstrip("/") != w:
                continue
            pub = {int(p["pub"]) for p in c.get("ports") or [] if isinstance(p, dict) and p.get("pub")
                   and (not ports or p.get("priv") in ports)}
            return pub
    return ports or set(open_ports.CF_PORTS)


def decided(srv: Server) -> set[str]:
    """Веб-серверы, про которые человек отметил "так задумано"."""
    return {m[len(MUTE_PREFIX):] for m in (getattr(srv, "alert_mutes", None) or []) if m.startswith(MUTE_PREFIX)}


def _hdr(x: dict) -> str:
    h = _strs(x.get("hdr"))
    return h[0] if h else "X-Forwarded-For"


def findings(srv: Server, now: datetime, with_decided: bool = False) -> list[dict]:
    """Веб-серверы, чьи белые списки обходятся подменой заголовка при прямом заходе."""
    items = block(srv.last_report or {}, now)
    if not items:
        return []
    cf = open_ports.cf_open(srv, now)
    if not cf:
        return []
    done = decided(srv)
    ctrls = podnames.ctrls(srv.last_report)
    by: dict[str, dict] = {}
    for x in items:
        trust = trust_any(x)
        if not trust:
            continue
        ports = sorted(p for p in cf if p in host_ports(srv, x))
        if not ports:
            continue
        k = key(x, ctrls)
        # решение, поставленное до того, как агент прислал владельца, - по имени без суффиксов
        mark = k in done or key(x) in done
        if mark and not with_decided:
            continue
        f = by.get(k)
        if f is None:
            by[k] = {"key": k, "label": label(x), "where": where_text(x, ctrls), "hdr": _hdr(x), "trust": trust,
                     "ports": ports, "acl": int(x.get("acl") or 0), "names": _strs(x.get("names"))[:3],
                     "decided": mark, "pods": 1 if _pod(x) else 0}
            continue
        # еще одна реплика того же ворклоада: конфиг у них общий, посреди выкладки может
        # отличаться - берем объединение
        f["pods"] += 1
        f["ports"] = sorted(set(f["ports"]) | set(ports))
        f["acl"] = max(f["acl"], int(x.get("acl") or 0))
        f["names"] = list(dict.fromkeys(f["names"] + _strs(x.get("names"))))[:3]
    return list(by.values())


def _ports(ports: list[int]) -> str:
    return " и ".join(str(p) for p in ports)


def level(f: dict) -> int:
    """2 - есть allow/deny, которые обходятся подменой; 1 - подделывается только адрес клиента."""
    return 2 if int(f.get("acl") or 0) > 0 else 1


def text(f: dict) -> str:
    """'белые списки обходятся подменой CF-Connecting-IP: ingress-nginx в поде default/ingress-nginx-controller
    верит заголовку от любого адреса (set_real_ip_from 0.0.0.0/0), а 80 и 443 открыты в обход Cloudflare'.
    Без allow/deny - 'IP клиента можно подделать заголовком X-Forwarded-For: ... - баны, лимиты входа и
    логи видят поддельный адрес'"""
    many = len(f["ports"]) > 1
    pods = f" (подов: {f['pods']})" if int(f.get("pods") or 0) > 1 else ""
    head = (f"белые списки обходятся подменой {f['hdr']}" if level(f) == 2
            else f"IP клиента можно подделать заголовком {f['hdr']}")
    tail = "" if level(f) == 2 else " - баны, лимиты входа и логи видят поддельный адрес"
    return (f"{head}: {f['label']} {f['where']}{pods} верит заголовку от любого адреса "
            f"({f['trust']}), а {_ports(f['ports'])} {'открыты' if many else 'открыт'} в обход Cloudflare{tail}")


def problems(srv: Server, now: datetime) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ) для "Что сломано": красное при allow/deny, иначе желтое."""
    return [(level(f), text(f), f["key"]) for f in findings(srv, now)]


def alerting(srv: Server, now: datetime) -> list[dict]:
    """Находки для алерта cf_realip: только те, где обходятся белые списки."""
    return [f for f in findings(srv, now) if level(f) == 2]


def view(srv: Server, now: datetime) -> list[dict] | None:
    """Для карточки "Что открыто в интернет": находки вместе с отмеченными "так задумано"."""
    if block(srv.last_report or {}, now) is None:
        return None
    return findings(srv, now, with_decided=True)


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Белые списки обходятся подменой CF-Connecting-IP
# ingress-nginx default/ingress-nginx-controller: set_real_ip_from 0.0.0.0/0
# 80 и 443 открыты в обход Cloudflare

def tg_short(items) -> tuple[str, list[str]] | None:
    rows = [x for x in items or [] if isinstance(x, dict) and x.get("label")]
    if not rows:
        return None
    r = rows[0]
    where = str(r.get("where") or "").removeprefix("в поде ").removeprefix("в контейнере ")
    # имя пода и так называет веб-сервер (default/ingress-nginx-controller) - без повтора
    who = r["label"] if where == "на хосте" else where if r["label"] in where else f"{r['label']} {where}"
    if int(r.get("pods") or 0) > 1:
        who += f" (подов: {r['pods']})"
    ports = r.get("ports") or []
    facts = [f"{who}: {r.get('trust')}",
             f"{_ports(ports)} {'открыты' if len(ports) > 1 else 'открыт'} в обход Cloudflare"]
    if len(rows) > 1:
        facts[1] += ", еще " + tg_fmt.names(x["label"] for x in rows[1:])
    return f"Белые списки обходятся подменой {r.get('hdr')}", facts
