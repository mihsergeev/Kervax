"""Поиск по всему парку (Ctrl+K): серверы, мониторы, сайты, контейнеры, порты, базы и сервисы,
очереди, поды, пользователи и SSH-ключи, юниты, кроны, версии ПО.

Отдельно ничего не копим: все берется из последних отчетов агентов. Записи сервера строятся
один раз на каждый новый отчет и живут в памяти процесса, сам поиск - подстроки по ним, без БД.
Из отчета читаются только нужные ветки, а не весь JSON (у большой ноды он за 200 КБ).

Ответ - группы по видам, в каждой лучшие PER_KIND записей и сколько нашлось всего. Подписи
(sub, tag, hint) - это данные и ключи, слова к ним подставляет фронт на языке учетки.
"""

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app.models import Server

# порядок групп в ответе: сначала сами объекты, потом то, что на них нашлось
KINDS = ("server", "check", "site", "container", "port", "db", "queue", "pod",
         "user", "key", "unit", "cron", "version")
# раздел панели, без которого вид не показываем (и куда ведет клик)
SECTION = {
    "server": "servers", "site": "servers", "port": "servers", "user": "servers", "key": "servers",
    "unit": "servers", "cron": "servers", "version": "servers",
    "container": "docker", "pod": "kuber", "db": "services", "queue": "services", "check": "sites",
}
PER_KIND = 8
MIN_LEN = 2
_PORT = re.compile(r"^:?(\d{1,5})$")
# служебные базы есть почти везде, по ним искать незачем
_SYSTEM_DBS = {"postgres", "template0", "template1", "system", "default", "information_schema",
               "mysql", "performance_schema", "sys", "admin", "local", "config", "_system"}
# порты, которые docker пробрасывает своим прокси: по имени процесса это шум на каждой ноде
_NOISY_PROC = {"docker-proxy"}

# ветки отчета, которые нужны поиску
_PARTS = {
    "containers": ("docker", "containers"),
    "docker_v": ("docker", "version"),
    "compose_v": ("docker", "compose"),
    "listen": ("listen",),
    "web": ("web_services",),
    "ips": ("ips",),
    "os": ("os",),
    "hostname": ("hostname",),
    "db_stats": ("db_stats",),
    "db_engines": ("db_engines",),
    "services": ("services",),
    "pods": ("kube", "pods"),
    "changes": ("extras", "changes"),
    "kernel": ("os_upd", "kernel"),
}


@dataclass
class Entry:
    kind: str
    title: str
    id: int
    sub: list[str] = field(default_factory=list)
    sec: str = ""
    tag: str = ""
    # по чему ищем: (ключ подписи, значение); ключ "" - само название, подсказка не нужна
    fields: list[tuple[str, str]] = field(default_factory=list)
    port: int = 0


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def _l(x) -> list:
    return x if isinstance(x, list) else []


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _sub(*parts) -> list[str]:
    return [str(p) for p in parts if p not in (None, "")]


def build_server(srv, rep: dict) -> list[Entry]:
    """Все записи одного сервера. srv - строка с колонками сервера, rep - ветки отчета."""
    sid, name = srv.id, srv.name
    out: list[Entry] = []
    ips: list[str] = []
    for ip in (srv.external_ip, srv.agent_ip, srv.local_ip, *_l(rep.get("ips"))):
        ip = _s(ip)
        if ip and ip not in ips:
            ips.append(ip)
    os_name = _s(srv.os) or _s(rep.get("os"))
    f = [("", name), ("hostname", _s(srv.hostname)), ("hostname", _s(rep.get("hostname"))),
         ("", _s(srv.group_name)), ("note", _s(srv.note))]
    f += [("ip", ip) for ip in ips]
    out.append(Entry("server", name, sid, _sub(srv.group_name, ips[0] if ips else "", os_name), fields=f))

    seen_sites: set[str] = set()
    for w in _l(rep.get("web")):
        w = _d(w)
        for site in _l(w.get("sites")):
            site = _s(site)
            if not site or site.startswith("~") or site in seen_sites:
                continue
            seen_sites.add(site)
            out.append(Entry("site", site, sid, _sub(name, _s(w.get("kind"))), sec="web", fields=[("", site)]))

    for c in _l(rep.get("containers")):
        c = _d(c)
        cname, image = _s(c.get("name")), _s(c.get("image"))
        if not cname:
            continue
        # образ и так в подписи, подсказка нужна только для пути
        f = [("", cname), ("", image)] + [("path", _s(b)) for b in _l(c.get("binds"))]
        out.append(Entry("container", cname, sid, _sub(name, image, _s(c.get("state"))), fields=f))

    procs = {str(k): _s(v) for k, v in _d(_d(rep.get("changes")).get("listen")).items()}
    ports: dict[tuple[int, str], dict] = {}
    for x in _l(rep.get("listen")):
        x = _d(x)
        try:
            port = int(x.get("port"))
        except (TypeError, ValueError):
            continue
        p = ports.setdefault((port, _s(x.get("p")) or "tcp"), {"ips": [], "u": _s(x.get("u"))})
        ip = _s(x.get("ip"))
        if ip and ip not in p["ips"]:
            p["ips"].append(ip)
    for (port, proto), p in sorted(ports.items()):
        proc = procs.get(str(port), "")
        f = [("process", proc)] if proc and proc not in _NOISY_PROC else []
        out.append(Entry("port", f":{port}", sid, _sub(name, proto, proc or p["u"], ", ".join(p["ips"])),
                         sec="conn", fields=f, port=port))

    engines_seen: set[str] = set()
    for d in _l(rep.get("db_stats")):
        d = _d(d)
        eng, ver, cont = _s(d.get("engine")), _s(d.get("version")), _s(d.get("container"))
        if not eng:
            continue
        engines_seen.add(eng.lower())
        title = f"{eng} {ver}".strip()
        out.append(Entry("db", title, sid, _sub(name, cont), tag="engine",
                         fields=[("", title), ("", cont)]))
        for db in _l(d.get("dbs")):
            dbn = _s(_d(db).get("name"))
            if dbn and dbn.lower() not in _SYSTEM_DBS:
                out.append(Entry("db", dbn, sid, _sub(name, eng, cont), tag="database", fields=[("", dbn)]))
    for eng in _l(rep.get("db_engines")):
        eng = _s(eng)
        if eng and eng.lower() not in engines_seen:
            out.append(Entry("db", eng, sid, _sub(name), tag="engine", fields=[("", eng)]))

    for svc in _l(rep.get("services")):
        svc = _d(svc)
        kind, source = _s(svc.get("kind")), _s(svc.get("source"))
        if not kind:
            continue
        out.append(Entry("db", kind, sid, _sub(name, source), tag="service", fields=[("", kind)]))
        for q in _l(svc.get("queues")):
            q = _d(q)
            qn = _s(q.get("name"))
            if qn:
                vhost = _s(q.get("vhost"))
                out.append(Entry("queue", qn, sid, _sub(name, kind, vhost if vhost != "/" else ""),
                                 fields=[("", qn)]))

    ctrls: dict[tuple[str, str], dict] = {}
    for p in _l(rep.get("pods")):
        p = _d(p)
        pname, ns = _s(p.get("name")), _s(p.get("ns"))
        if not pname:
            continue
        c = ctrls.setdefault((ns, _s(p.get("ctrl")) or pname), {"pods": [], "bad": 0})
        c["pods"].append(pname)
        if not p.get("ready", True) or _s(p.get("phase")) not in ("", "Running", "Succeeded"):
            c["bad"] += 1
    for (ns, ctrl), c in ctrls.items():
        f = [("", ctrl), ("ns", ns)] + [("pod", pn) for pn in c["pods"] if pn != ctrl]
        out.append(Entry("pod", ctrl, sid, _sub(name, ns, f"{len(c['pods'])}"), tag="bad" if c["bad"] else "",
                         fields=f))

    ch = _d(rep.get("changes"))
    for u in _l(ch.get("users")):
        u = _d(u)
        un = _s(u.get("n"))
        if un:
            out.append(Entry("user", un, sid, _sub(name, f"uid {u.get('uid')}", _s(u.get("sh"))), sec="proc",
                             fields=[("", un)]))
    keys_seen: set[str] = set()
    for k in _l(ch.get("keys")):
        k = _d(k)
        fp, comment = _s(k.get("fp")), _s(k.get("c"))
        if not fp or fp in keys_seen:
            continue
        keys_seen.add(fp)
        out.append(Entry("key", comment or fp, sid, _sub(name, _s(k.get("u")), _s(k.get("t"))), sec="proc",
                         fields=[("", comment), ("fp", fp)]))
    for unit in _l(ch.get("units")):
        unit = _s(unit)
        if unit:
            out.append(Entry("unit", unit, sid, _sub(name), sec="proc", fields=[("", unit)]))
    for path in _d(ch.get("cron")):
        path = _s(path)
        if path:
            out.append(Entry("cron", path, sid, _sub(name), sec="proc", fields=[("", path)]))

    versions = (
        ("os", os_name),
        ("kernel", _s(rep.get("kernel"))),
        ("ssh", _s(_d(ch.get("ssh")).get("banner"))),
        ("docker", _s(rep.get("docker_v"))),
        ("compose", _s(rep.get("compose_v"))),
        ("agent", _s(srv.agent_version)),
    )
    for tag, val in versions:
        if val:
            # метка тоже ищется: "docker 27" находит версии docker, а не все, где есть "27"
            out.append(Entry("version", val, sid, _sub(name), tag=tag, fields=[("", val), ("", tag)]))
    return out


def _no_userinfo(target: str) -> str:
    # https://user:pass@host/ - логин и пароль в подпись не тащим
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", target, flags=re.I)


def check_entry(c) -> Entry:
    target = _no_userinfo(_s(c.target))
    return Entry("check", c.name, c.id, _sub(c.type, target),
                 fields=[("", c.name), ("addr", target), ("group", _s(c.group_name)), ("note", _s(c.note))])


# --- кэш записей по серверам ------------------------------------------------------------------

_SRV_COLS = (Server.id, Server.name, Server.group_name, Server.hostname, Server.os, Server.agent_version,
             Server.local_ip, Server.external_ip, Server.agent_ip, Server.note, Server.last_seen)
_cache: dict[int, tuple[tuple, list[Entry]]] = {}


def _stamp(r) -> tuple:
    # новый отчет (last_seen) или правка сервера в панели - записи строим заново
    return tuple(getattr(r, c.key) for c in _SRV_COLS[1:])


async def server_entries(session, rows, full: bool = False) -> list[Entry]:
    """Записи серверов rows (строки из select(*_SRV_COLS)). full - учетка видит все серверы:
    тогда заодно выкидываем из кэша удаленные."""
    stale = [r for r in rows if _cache.get(r.id, ((),))[0] != _stamp(r)]
    if stale:
        cols = [Server.last_report[path if len(path) > 1 else path[0]] for path in _PARTS.values()]
        got = {row[0]: row[1:] for row in (await session.execute(
            select(Server.id, *cols).where(Server.id.in_([r.id for r in stale]))
        )).all()}
        for r in stale:
            rep = dict(zip(_PARTS, got.get(r.id) or [None] * len(_PARTS)))
            _cache[r.id] = (_stamp(r), build_server(r, rep))
    if full:
        alive = {r.id for r in rows}
        for sid in [k for k in _cache if k not in alive]:
            _cache.pop(sid, None)
    out: list[Entry] = []
    for r in rows:
        out += _cache[r.id][1]
    return out


# --- сам поиск --------------------------------------------------------------------------------

def _score(e: Entry, toks: list[str]) -> tuple[int, tuple[str, str] | None]:
    """Каждое слово запроса должно найтись хоть в одном поле записи. Совпадение целиком - 3,
    с начала - 2, внутри - 1. Подсказка - поле с подписью, где нашлось слово не из названия."""
    total, hint = 0, None
    title = e.title.lower()
    for t in toks:
        best, best_f = 0, None
        for key, val in [("", e.title), *e.fields]:
            v = val.lower()
            if not v or t not in v:
                continue
            sc = 3 if v == t else 2 if v.startswith(t) else 1
            if sc > best:
                best, best_f = sc, (key, val)
        if not best:
            return 0, None
        total += best
        if hint is None and best_f and best_f[0] and t not in title:
            hint = best_f
    return total, hint


def find(entries: list[Entry], q: str, sections: set[str]) -> dict:
    q = (q or "").strip()
    out = {"q": q, "groups": []}
    if len(q) < MIN_LEN:
        return out
    toks = q.lower().split()
    m = _PORT.match(q)
    port = int(m.group(1)) if m else None
    found: dict[str, list[tuple[int, Entry, tuple | None]]] = {}
    for e in entries:
        if SECTION[e.kind] not in sections:
            continue
        if e.kind == "port" and port is not None:
            # номер порта - только точное совпадение, иначе "80" найдет и 8080, и 1808
            if e.port == port:
                found.setdefault("port", []).append((3, e, None))
            continue
        sc, hint = _score(e, toks)
        if sc:
            found.setdefault(e.kind, []).append((sc, e, hint))
    for kind in KINDS:
        rows = found.get(kind)
        if not rows:
            continue
        rows.sort(key=lambda x: (-x[0], x[1].title.lower(), x[1].sub))
        out["groups"].append({
            "kind": kind,
            "total": len(rows),
            "items": [{
                "title": e.title, "sub": e.sub, "id": e.id, "section": SECTION[kind], "sec": e.sec,
                "tag": e.tag, "hint_k": h[0] if h else "", "hint_v": h[1] if h else "",
            } for _, e, h in rows[:PER_KIND]],
        })
    return out
