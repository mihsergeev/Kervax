"""OOM killer: эпизоды нехватки памяти на сервере.

Раньше OOM был виден только как упавшая служба, и обычно не та: ядро сначала убивает процессы с
самым высоким oom_score_adj (менеджер сессии пользователя, user@N.service, у него 100), и в боте
приходило "упал user@1001.service: убит сигналом KILL", а кто съел память и что убили ради нее,
видно не было. Если OOM убил только дочерний процесс внутри живой службы, не приходило ничего.
Счетчик убийств агента (дельта за интервал) при этом терялся: на OOM цикл агента висит, вотчдог
его перезапускает, и убийство попадает между двумя процессами.

Теперь:
- агент 2.42 шлет сам счетчик ядра (oom_total), рост считает панель от прошлого отчета
  (counter_step) - убийство не теряется, даже если агент перезапустился или отчет не дошел;
- helper units-setup 0.5 разбирает журнал ядра по курсору (агент от kervax его не читает) и
  кладет в report.d/oom.json убийства за неделю: кого убили, в какой cgroup, таблицу процессов
  (кто занимал память, rss_anon по имени и uid), Mem-Info, отметки systemd "в юните убит
  процесс" и "упал с результатом oom-kill", убийства systemd-oomd;
- панель хранит убийства 30 дней (oom_kills), собирает из них эпизоды (убийства с промежутком
  до минуты), шлет один алерт на эпизод и привязывает к нему упавшие юниты.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OomKill, Server

EXTRA_KEY = "oom"
# мьют по виновнику: "oom:<ключ виновника>" (culprit)
MUTE_PREFIX = "oom:"
# убийства с промежутком до минуты - один эпизод (на OOM ядро убивает несколько процессов подряд)
GAP = 60
KEEP_DAYS = 30
# блок helper'а старше этого не в счет: helper молчит, о свежем OOM судить не по чему
BLOCK_MAX_AGE = 10 * 60
# эпизод закрыт, когда после последнего убийства прошло столько: раньше алерт ушел бы без
# следующих убийств того же эпизода (helper читает журнал раз в минуту)
SETTLE = 90
# рост счетчика ядра без эпизода в журнале: столько ждем данных helper'а, потом алерт без подробностей
WAIT_DETAILS = 300
# повторы: следующий эпизод в течение часа после сообщения не шлется, через час - одно сообщение
# с количеством
REPEAT_S = 3600
# отметки systemd о юните относятся к эпизоду в этом окне: время строк ядра в журнале - когда их
# прочитал journald, а он под нехваткой памяти отстает (на одном сервере строка ядра пришла через
# 76 секунд после отметки systemd)
UNIT_WINDOW = 180
# Сообщение ушло позже конца эпизода больше чем на столько - в тексте пометка "с опозданием": иначе
# придержанный на час повтор или OOM, пока панель лежала, читались как свежая беда. Обычный путь
# (журнал раз в минуту + SETTLE) - пара минут.
LATE_S = 600
# Единичный OOM в "Что сломано" держится столько после конца: это событие, а не поломка, и строка
# "была 17 ч назад" висела сутки. Повторы и лежащая после OOM служба - дольше (problem)
HOME_ONE_S = 3600

# Процессы, убийство которых - авария, а не мелочь: базы, докер, веб-серверы, брокеры. Убитый
# воркер php-fpm поднимется сам, убитый mysqld - это лежащая база.
IMPORTANT = frozenset({
    "mysqld", "mariadbd", "postgres", "postmaster", "mongod", "redis-server", "clickhouse-server",
    "clickhouse", "dockerd", "containerd", "nginx", "apache2", "httpd", "haproxy", "caddy",
    "traefik", "beam.smp", "etcd", "kubelet", "kube-apiserver", "elasticsearch", "memcached",
})

_SVC = re.compile(r"^/(?:[^/]+\.slice/)*([^/]+\.service)(?:/|$)")
_DOCKER = re.compile(r"(?:docker-|/docker/)([0-9a-f]{12,64})")
_POD = re.compile(r"pod([0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})")
_USER = re.compile(r"^/user\.slice/user-(\d+)\.slice")
_SCOPE = re.compile(r"/([^/]+\.scope)$")
_QUIET_UNIT = re.compile(r"user(?:-runtime-dir)?@\d+\.service")


def counter_step(prev: dict, body) -> tuple[int, int]:
    """(сколько убийств с прошлого отчета, сколько еще не попало в точку графика). Агент 2.42 шлет
    счетчик ядра, рост берем от прошлого отчета; после перезагрузки счетчик с нуля. Старый агент
    и первый отчет с счетчиком - дельта агента за интервал, как раньше."""
    delta = round(body.oom_kill or 0)
    cur = getattr(body, "oom_total", None)
    pt = prev.get("oom_total")
    if isinstance(cur, int) and isinstance(pt, int):
        pu = prev.get("uptime_seconds") or 0
        rebooted = bool(pu and body.uptime_seconds and body.uptime_seconds < pu - 60)
        delta = cur if rebooted else max(0, cur - pt)
    return delta, int(prev.get("oom_pend") or 0) + delta


# -- блок helper'а --

def block(rep: dict, now_ts: float | None = None) -> dict | None:
    """Свежий блок report.d/oom.json из отчета или None."""
    b = ((rep or {}).get("extras") or {}).get(EXTRA_KEY)
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = _f(b.get("ts"))
    ref = now_ts or _f(rep.get("clock_unix")) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > BLOCK_MAX_AGE:
        return None
    return b


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x) -> int | None:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _s(x, n: int = 200) -> str:
    return str(x or "")[:n]


def records(b: dict | None) -> list[dict]:
    """Строки блока в одном виде: убийства (kind global/memcg/oomd) и отметки юнитов (unit).
    Кривые строки выбрасываются."""
    out: list[dict] = []
    if not isinstance(b, dict):
        return out
    for k in b.get("kills") or []:
        if not isinstance(k, dict) or _f(k.get("ts")) <= 0:
            continue
        kind = k.get("kind") if k.get("kind") in ("global", "memcg", "oomd") else "global"
        mem = k.get("mem") if isinstance(k.get("mem"), dict) else {}
        top = [{"name": _s(t.get("name"), 64), "uid": _i(t.get("uid")), "user": _s(t.get("user"), 32),
                "n": _i(t.get("n")) or 0, "anon": _i(t.get("anon")) or 0}
               for t in (k.get("top") or [])[:6] if isinstance(t, dict)]
        out.append({
            "kind": kind, "ts": _f(k.get("ts")), "pid": _i(k.get("pid")), "name": _s(k.get("name"), 64),
            "uid": _i(k.get("uid")), "user": _s(k.get("user"), 32), "anon": _i(k.get("anon")),
            "file": _i(k.get("file")), "shmem": _i(k.get("shmem")), "adj": _i(k.get("adj")),
            "constraint": _s(k.get("constraint"), 40), "memcg": _s(k.get("memcg"), 300),
            "oom_memcg": _s(k.get("oom_memcg"), 300), "by": _s(k.get("by"), 64),
            "mem": {x: _i(mem.get(x)) for x in ("anon", "file", "shmem", "swap_free", "swap_total", "ram", "limit")},
            "top": top, "line": _s(k.get("line"), 240),
        })
    for u in b.get("units") or []:
        if isinstance(u, dict) and _f(u.get("ts")) > 0 and u.get("unit"):
            out.append({"kind": "unit", "ts": _f(u.get("ts")), "unit": _s(u.get("unit"), 200),
                        "what": "failed" if u.get("what") == "failed" else "oom"})
    return out


def rec_key(r: dict) -> str:
    if r["kind"] == "unit":
        return f"u:{r['ts']:.6f}:{r['what']}:{r['unit']}"[:120]
    return f"k:{r['ts']:.6f}:{r.get('pid') or 0}:{r.get('memcg') or ''}"[:120]


# -- хранение --

_STORED: dict[int, float] = {}  # сервер -> время самой свежей записанной строки (в этом процессе)


async def store(session: AsyncSession, server: Server, extras: dict | None, now: datetime) -> int:
    """Новые строки блока в oom_kills. Блок каждый отчет один и тот же, пока не было новых
    убийств: без новых строк в базу не ходим."""
    recs = records((extras or {}).get(EXTRA_KEY) if isinstance(extras, dict) else None)
    if not recs:
        return 0
    newest = max(r["ts"] for r in recs)
    if _STORED.get(server.id, 0) >= newest:
        return 0
    since = datetime.fromtimestamp(min(r["ts"] for r in recs), timezone.utc) - timedelta(seconds=1)
    have = set((await session.execute(select(OomKill.key).where(
        OomKill.server_id == server.id, OomKill.ts >= since))).scalars())
    n = 0
    for r in recs:
        key = rec_key(r)
        if key in have:
            continue
        have.add(key)
        data = {k: v for k, v in r.items() if k not in ("kind", "ts")}
        session.add(OomKill(server_id=server.id, ts=datetime.fromtimestamp(r["ts"], timezone.utc),
                            key=key, kind=r["kind"], data=data))
        n += 1
    _STORED[server.id] = newest
    return n


async def load(session: AsyncSession, server_id: int, days: int = KEEP_DAYS) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await session.execute(select(OomKill).where(
        OomKill.server_id == server_id, OomKill.ts >= since).order_by(OomKill.ts))).scalars().all()
    out = []
    for row in rows:
        ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        out.append({**(row.data or {}), "kind": row.kind, "ts": ts.timestamp()})
    return out


async def prune(session: AsyncSession, now: datetime) -> None:
    await session.execute(delete(OomKill).where(OomKill.ts < now - timedelta(days=KEEP_DAYS)))


# -- эпизоды --

def owner(memcg: str, user: str = "", uid: int | None = None) -> dict:
    """Чья это cgroup: служба, контейнер, сессия пользователя, под, scope или весь сервер."""
    cg = memcg or ""
    if "kubepods" in cg:
        m = _POD.search(cg)
        return {"kind": "pod", "name": m.group(1).replace("_", "-") if m else ""}
    m = _DOCKER.search(cg)
    if m:
        return {"kind": "container", "name": m.group(1)[:12]}
    m = _USER.match(cg)
    if m:
        return {"kind": "user", "name": user or m.group(1), "uid": int(m.group(1))}
    m = _SVC.match(cg)
    if m:
        return {"kind": "service", "name": m.group(1)}
    m = _SCOPE.search(cg)
    if m:
        return {"kind": "scope", "name": m.group(1)}
    return {"kind": "host" if cg in ("", "/") else "other", "name": "" if cg in ("", "/") else cg}


def owner_text(o: dict, containers: dict[str, str] | None = None) -> str:
    k, n = o.get("kind"), o.get("name") or ""
    if k == "container":
        return "контейнер " + ((containers or {}).get(n) or n)
    if k == "pod":
        return "под " + (n[:8] if n else "")
    if k == "user":
        return "сессия " + n
    return n


def episodes(recs: list[dict]) -> list[dict]:
    """Убийства в эпизоды по порядку времени. Таблица процессов и Mem-Info - первого убийства, где
    они есть: она снята до первого убийства, когда память еще занята виновником. Отметки systemd
    о юнитах - в окне эпизода."""
    kills = sorted((r for r in recs if r.get("kind") != "unit"), key=lambda r: r["ts"])
    marks = [r for r in recs if r.get("kind") == "unit"]
    eps: list[dict] = []
    for k in kills:
        if eps and k["ts"] - eps[-1]["end"] <= GAP:
            eps[-1]["kills"].append(k)
            eps[-1]["end"] = k["ts"]
        else:
            eps.append({"start": k["ts"], "end": k["ts"], "kills": [k]})
    for e in eps:
        e["id"] = str(int(e["start"]))
        kinds = {k["kind"] for k in e["kills"]}
        e["kind"] = "global" if "global" in kinds else ("memcg" if "memcg" in kinds else "oomd")
        e["top"] = next((k["top"] for k in e["kills"] if k.get("top")), [])
        e["mem"] = next((k["mem"] for k in e["kills"] if any(v for v in (k.get("mem") or {}).values())), {})
        e["by"] = sorted({k["by"] for k in e["kills"] if k.get("by")})
        units: dict[str, set[str]] = {}
        for m in marks:
            if e["start"] - UNIT_WINDOW <= m["ts"] <= e["end"] + UNIT_WINDOW:
                units.setdefault(m["unit"], set()).add(m["what"])
        e["units"] = {u: sorted(w) for u, w in sorted(units.items())}
    # служба процесса по всем убийствам периода: виновника в его эпизоде часто не убивают (ядро
    # убило mysqld, а память у воркеров php-fpm), зато его процессы убиты в соседнем
    procs: dict[tuple, dict] = {}
    for k in kills:
        o = kill_owner(k)
        if o["kind"] != "user" and _owner_key(o):
            procs[(k.get("name"), k.get("uid"))] = o
    for e in eps:
        e["procs"] = procs
    return eps


def kill_owner(k: dict) -> dict:
    return owner(k.get("memcg") or "", k.get("user") or "", k.get("uid"))


def size_kb(kb: float | int | None) -> str:
    """Коротко, как пишут в чате: 567мб, 5.8гб, 900кб."""
    kb = float(kb or 0)
    if kb >= 1024 * 1024:
        v = kb / 1024 / 1024
        return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}") + "гб"
    if kb >= 1024:
        return f"{kb / 1024:.0f}мб"
    return f"{kb:.0f}кб"


def _user_of(k: dict) -> str:
    return k.get("user") or (str(k["uid"]) if k.get("uid") is not None else "")


def kills_text(e: dict, containers: dict[str, str] | None = None) -> str:
    """python 554мб (php7.4-fpm.service, uid web), systemd + sd-pam (сессия web): по владельцу,
    крупное с размером. Процессы сессии пользователя - мелочь, их только по имени."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for k in e["kills"]:
        o = kill_owner(k)
        key = f"{o['kind']}:{o.get('name')}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(k)
    # крупное первым: ради него и убивали
    order.sort(key=lambda key: -sum(k.get("anon") or 0 for k in groups[key]))
    parts = []
    for key in order:
        ks = groups[key]
        o = kill_owner(ks[0])
        names: list[str] = []
        for k in ks:
            nm = (k.get("name") or "").strip("()") or ("cgroup" if k["kind"] == "oomd" else "?")
            if nm not in names:
                names.append(nm)
        anon = sum(k.get("anon") or 0 for k in ks)
        what = " + ".join(names)
        if o["kind"] != "user" and anon >= 10 * 1024:
            what += " " + size_kb(anon)
        where = owner_text(o, containers)
        u = _user_of(ks[0])
        if u and o["kind"] in ("service", "scope", "other", "host"):
            where = f"{where}, uid {u}" if where else f"uid {u}"
        parts.append(f"{what} ({where})" if where else what)
    return ", ".join(parts)


def top_text(e: dict, limit: int = 3) -> str:
    """python x12 5.8гб (php7.4-fpm), postgres x41 2.3гб: кто занимал память перед убийством.
    Служба - у процессов, которые убивали в этом эпизоде (в таблице ядра cgroup нет)."""
    where: dict[tuple, str] = {}
    for k in e["kills"]:
        o = kill_owner(k)
        if o["kind"] == "service":
            where[(k.get("name"), k.get("uid"))] = o["name"].removesuffix(".service")
    parts = []
    for t in e.get("top") or []:
        if (t.get("anon") or 0) < 10 * 1024:
            continue
        s = t["name"] + (f" x{t['n']}" if (t.get("n") or 0) > 1 else "") + " " + size_kb(t["anon"])
        w = where.get((t["name"], t.get("uid")))
        parts.append(f"{s} ({w})" if w else s)
        if len(parts) >= limit:
            break
    return ", ".join(parts)


def mem_text(e: dict) -> str:
    m = e.get("mem") or {}
    if e["kind"] == "memcg":
        lim = m.get("limit")
        return f"лимит cgroup {size_kb(lim)}" if lim else ""
    parts = []
    if m.get("ram"):
        parts.append(f"RAM {size_kb(m['ram'])}")
    st, sf = m.get("swap_total"), m.get("swap_free")
    if st == 0:
        parts.append("swap нет")
    elif st:
        parts.append(f"swap занят {size_kb(st - (sf or 0))} из {size_kb(st)}")
    return ", ".join(parts)


def cg_units(memcg: str) -> set[str]:
    """Юниты systemd, внутри которых лежит cgroup: у /user.slice/user-1001.slice/user@1001.service/init.scope
    это user@1001.service и init.scope. Владелец такой cgroup - "сессия idv", а упал юнит user@1001.service."""
    return {p for p in (memcg or "").split("/") if p.endswith((".service", ".scope"))}


def quiet_unit(name: str) -> bool:
    """Менеджер сессии пользователя: убит OOM - не служба упала, он поднимется при следующем входе."""
    return bool(_QUIET_UNIT.fullmatch(name or ""))


def failed_units(e: dict, failed_now: set[str]) -> list[tuple[str, bool]]:
    """Юниты, которые упали из-за эпизода: (юнит, лежит ли сейчас). Сессии пользователя не в счет."""
    out = []
    for u, what in (e.get("units") or {}).items():
        if quiet_unit(u):
            continue
        if "failed" in what or u in failed_now:
            out.append((u, u in failed_now))
    return out


def level(e: dict, failed_now: set[str]) -> int:
    """2 - убит основной процесс важной службы или юнит упал и не поднялся, 1 - остальное."""
    if any((k.get("name") or "") in IMPORTANT for k in e["kills"]):
        return 2
    if any(down for _u, down in failed_units(e, failed_now)):
        return 2
    return 1


def hhmm(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%H:%M")


def late_text(e: dict, now_ts: float | None, held: bool = False) -> str:
    """Пометка для эпизода, о котором сообщаем поздно: "с опозданием на 1 ч 47 мин" и почему, если
    знаем. Не поздно (или время не дали) - пусто."""
    if now_ts is None or now_ts - e["end"] < LATE_S:
        return ""
    m = int((now_ts - e["end"]) // 60)
    lag = f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"
    why = ": повторы в течение часа после прошлого сообщения собираются в одно" if held else ""
    return f"с опозданием на {lag}{why}"


def episode_text(e: dict, tz, failed_now: set[str], n7: int, containers: dict[str, str] | None = None,
                 more: int = 0, late: str = "") -> str:
    """Текст алерта по эпизоду, строки:
    нехватка памяти (OOM), 12:22
    убиты: python 554мб (php7.4-fpm.service, uid web), systemd + sd-pam (сессия web)
    больше всего памяти: python x12 5.8гб (php7.4-fpm), postgres x41 2.3гб
    RAM 8гб, swap нет
    за 7 дней OOM 13 раз"""
    at = hhmm(e["start"], tz)
    if e["kind"] == "memcg":
        k = next((k for k in e["kills"] if k["kind"] == "memcg"), e["kills"][0])
        o = owner(k.get("oom_memcg") or k.get("memcg") or "", k.get("user") or "", k.get("uid"))
        head = f"кончилась память в лимите {owner_text(o, containers) or 'cgroup'} (OOM), {at}"
    elif e["kind"] == "oomd":
        head = f"systemd-oomd убил процессы из-за нехватки памяти, {at}"
    else:
        head = f"нехватка памяти (OOM), {at}"
    if more:
        head += f", с прошлого сообщения еще {more} раз"
    if late:
        head += f" - {late}"
    lines = [head, "убиты: " + kills_text(e, containers)]
    fu = failed_units(e, failed_now)
    if fu:
        lines.append("упали: " + ", ".join(f"{u} ({'не поднялась' if down else 'поднялась'})" for u, down in fu))
    tt = top_text(e)
    if tt:
        lines.append("больше всего памяти: " + tt)
    mt = mem_text(e)
    if mt:
        lines.append(mt)
    if n7 > 1:
        lines.append(f"за 7 дней OOM {n7} раз")
    return "\n".join(lines)


# -- юниты --

def unit_link(u: dict, eps: list[dict]) -> dict | None:
    """Юнит из блока упавших убит OOM: {"ep": id эпизода или None, "line": строка ядра}. Признаки:
    отметка systemd о юните в эпизоде, результат oom-kill, строка "killed by the OOM killer" в
    логе юнита, убитый процесс из cgroup этого юнита около момента падения."""
    name = str(u.get("unit") or "")
    since = _f(u.get("since"))
    by_log = any("killed by the OOM killer" in str(x) for x in u.get("log") or [])
    by_res = u.get("result") == "oom-kill"

    def near(e: dict) -> bool:
        return not since or e["start"] - 600 <= since <= e["end"] + 600

    def mine(k: dict) -> bool:
        # убит процесс из cgroup юнита или вложенной в него: systemd + sd-pam из
        # user@1001.service/init.scope, процесс сессии из session-5.scope
        return name in cg_units(k.get("memcg") or "")

    ep = next((e for e in reversed(eps) if name in (e.get("units") or {}) and near(e)), None)
    if ep is None:
        ep = next((e for e in reversed(eps) if near(e) and any(mine(k) for k in e["kills"])), None)
    if ep is None and not (by_log or by_res):
        return None
    line = ""
    if ep is not None:
        k = next((k for k in ep["kills"] if mine(k)), None)
        line = (k or ep["kills"][0]).get("line") or ""
    return {"ep": ep["id"] if ep else None, "line": line}


def unit_pending(u: dict, b: dict | None, ref: float) -> bool:
    """Юнит убит сигналом KILL, а helper еще не прочитал журнал ядра за окно вокруг падения: OOM это
    или нет, станет ясно на ближайших минутах, и алерт "упал юнит" до этого уходить не должен - иначе
    рядом с алертом OOM придет второй про тот же случай. Без блока helper'а ждать нечего, дольше
    BLOCK_MAX_AGE после падения не ждем."""
    if b is None or u.get("result") != "signal" or _i(u.get("status")) != 9:
        return False
    since = _f(u.get("since"))
    return since > 0 and _f(b.get("ts")) < since + UNIT_WINDOW and ref - since < BLOCK_MAX_AGE


def units_failed_now(rep: dict) -> set[str]:
    ub = ((rep or {}).get("extras") or {}).get("units") or {}
    return {str(u.get("unit")) for u in (ub.get("units") or []) if isinstance(u, dict) and u.get("unit")}


def containers_of(rep: dict) -> dict[str, str]:
    """id контейнера (12 знаков) -> имя, из отчета агента."""
    out = {}
    for c in ((rep or {}).get("docker") or {}).get("containers") or []:
        if isinstance(c, dict) and c.get("id") and c.get("name"):
            out[str(c["id"])[:12]] = str(c["name"])
    return out


# -- мьют --
# OOM, который повторяется, пока не починят виновника, глушится по нему: alert_mutes "oom:<виновник>"
# насовсем или alert_snoozes с тем же ключом на время, OOM из-за других служб продолжают приходить.
# Виновник - кто занимал память, а не кого убили: ядро убивает самый крупный процесс, и в эпизоде
# из-за php-fpm (x32 по 200мб) первым убитым был mysqld на 525мб. "oom" целиком глушит все OOM сервера.
# Заглушенный эпизод учитывается молча: когда мьют кончится, о старом не придет.

def _owner_key(o: dict, containers: dict[str, str] | None = None) -> str:
    k, n = o.get("kind"), o.get("name") or ""
    if not n or k == "user":
        return ""
    if k == "service":
        return "svc:" + n
    if k == "container":
        return "ct:" + ((containers or {}).get(n) or n)
    if k == "pod":
        return "pod:" + n
    return "cg:" + n


def culprit(e: dict, containers: dict[str, str] | None = None) -> str:
    """Ключ виновника эпизода (без "oom:"), "" - не понять. Лимит cgroup - сама cgroup. Нехватка на
    сервере - кто больше всех занимал память по таблице ядра, со службой (контейнером) по убитому
    процессу с тем же именем и uid: python x12 5.7гб (php7.4-fpm) -> svc:php7.4-fpm.service. Такой
    процесс не убит в этом эпизоде - служба по убийствам соседних (episodes, procs), нет и там - имя
    процесса из таблицы, но не убитый: в эпизоде, где php-fpm x30 занимал 6гб, первым убили mysqld,
    и виновником выходил mysql.service. Таблицы нет - служба крупнейшего убитого."""
    if e.get("kind") == "memcg":
        k = next((k for k in e["kills"] if k["kind"] == "memcg"), None)
        if k is not None:
            return _owner_key(owner(k.get("oom_memcg") or k.get("memcg") or "", k.get("user") or "", k.get("uid")),
                              containers)
    loud = [k for k in e["kills"] if kill_owner(k)["kind"] != "user"]
    top = next((t for t in e.get("top") or [] if (t.get("anon") or 0) >= 10 * 1024), None)
    if top is not None:
        k = next((k for k in loud if k.get("name") == top["name"] and k.get("uid") == top.get("uid")), None)
        o = kill_owner(k) if k is not None else (e.get("procs") or {}).get((top["name"], top.get("uid")))
        key = _owner_key(o, containers) if o else ""
        name = (top.get("name") or "").strip("()")
        return key or ("proc:" + name if name else "")
    big = max(loud, key=lambda k: k.get("anon") or 0) if loud else None
    key = _owner_key(kill_owner(big), containers) if big is not None else ""
    if key:
        return key
    name = ((big or {}).get("name") or "").strip("()")
    return "proc:" + name if name else ""


def culprit_text(key: str) -> str:
    """Виновник для текста, как в чипе мьюта карточки: php7.4-fpm.service, контейнер web, процесс java."""
    kind, _, name = key.partition(":")
    if not name:
        return ""
    return {"ct": "контейнер " + name, "pod": "под " + name[:8], "proc": "процесс " + name}.get(kind, name)


def mutes_now(s: Server, now: datetime) -> set[str]:
    """Заглушенное у сервера сейчас: насовсем и снузы, которые не кончились."""
    out = set(s.alert_mutes or [])
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) > now:
            out.add(str(k))
    return out


def episode_muted(e: dict, mutes: set[str], failed_now: set[str], containers: dict[str, str] | None = None) -> bool:
    """Заглушен ли эпизод. Мьют виновника не глушит эпизод, после которого служба легла и не
    поднялась: это уже не повтор известного, а лежащая служба, и алерта "упал юнит" по ней нет.
    У нехватки на сервере без таблицы процессов (старый журнал, обрезанный вывод ядра) кто занимал
    память, не знаем: OOM на сервере уже глушат по виновнику - такой эпизод считается его же, иначе
    старый эпизод стоял красным рядом с заглушенными."""
    if "oom" in mutes:
        return True
    key = culprit(e, containers)
    blind = e.get("kind") == "global" and not e.get("top")
    if not (key and MUTE_PREFIX + key in mutes) and not (blind and any(m.startswith(MUTE_PREFIX) for m in mutes)):
        return False
    return not any(down for _u, down in failed_units(e, failed_now))


# -- для карточки --

def episode_view(e: dict, containers: dict[str, str] | None = None, failed_now: set[str] | None = None,
                 mutes: set[str] | None = None) -> dict:
    kills = []
    for k in e["kills"]:
        o = kill_owner(k)
        kills.append({
            "ts": k["ts"], "pid": k.get("pid"), "name": k.get("name") or "", "uid": k.get("uid"),
            "user": k.get("user") or "", "anon": k.get("anon"), "adj": k.get("adj"), "kind": k["kind"],
            # имя без "контейнер"/"сессия": слово ставит карточка на своем языке
            "owner": (containers or {}).get(o.get("name") or "") or o.get("name") or "",
            "owner_kind": o["kind"], "memcg": k.get("memcg") or "",
            "line": k.get("line") or "",
        })
    return {
        "id": e["id"], "start": e["start"], "end": e["end"], "kind": e["kind"],
        "level": level(e, failed_now or set()), "kills": kills, "top": e.get("top") or [],
        "mem": e.get("mem") or {}, "by": e.get("by") or [], "units": e.get("units") or {},
        "text": kills_text(e, containers),
        # кто занимал память (ключ мьюта без "oom:"): по нему глушат повторы
        "culprit": culprit(e, containers),
        "muted": episode_muted(e, mutes or set(), failed_now or set(), containers),
    }


def view(s: Server, now: datetime) -> dict | None:
    """Коротко для списка и карточки: сколько эпизодов за 7 дней, последний, и юниты, которые
    убил OOM (ссылка на эпизод и строка ядра). Из блока helper'а в последнем отчете."""
    rep = s.last_report or {}
    b = block(rep, now.timestamp())
    eps = episodes(records(b)) if b else []
    week = [e for e in eps if e["start"] >= now.timestamp() - 7 * 86400]
    links = {}
    for u in (((rep.get("extras") or {}).get("units") or {}).get("units") or []):
        if isinstance(u, dict) and u.get("unit"):
            lk = unit_link(u, eps)
            if lk is not None:
                links[str(u["unit"])] = {**lk, "quiet": quiet_unit(str(u["unit"]))}
    if not week and not links:
        return None
    last = week[-1] if week else None
    conts = containers_of(rep)
    return {
        "n7": len(week), "last_at": last["start"] if last else None,
        "last_text": kills_text(last, conts) if last else "",
        "units": links,
        # виновник последнего эпизода: кнопка "не алертить" предлагает глушить по нему
        "culprit": culprit(last, conts) if last else "",
    }


# -- алерт --

def alert_step(s: Server, st: dict, rep: dict, now: datetime, tz,
               mutes: set[str] | None = None) -> tuple[tuple[int, str] | None, dict, dict]:
    """Что делать с алертом OOM на этом тике: (уровень и текст или None, что записать в состояние
    после доставки, что записать сразу; "oom_ep" в первом - эпизод для ссылки, в состояние не идет). Состояние:
    oom_end - конец последнего учтенного эпизода, oom_seen - учтенный счетчик ядра, oom_sent и
    oom_sent_lvl - когда и с каким уровнем ушло сообщение, oom_held - эпизоды, отложенные до
    конца часа после сообщения, oom_wait - когда вырос счетчик, а эпизода в журнале еще нет.
    mutes - заглушенное у сервера сейчас (mutes_now): заглушенные эпизоды учитываются молча."""
    now_ts = now.timestamp()
    ref = _f(rep.get("clock_unix")) or now_ts
    total = int(s.oom_total or 0)
    seen = int(st.get("oom_seen", 0) or 0)
    b = block(rep, ref)
    eps = episodes(records(b)) if b else []
    failed = units_failed_now(rep)
    conts = containers_of(rep)
    now_apply: dict = {}
    done = _f(st.get("oom_end"))
    if b is not None and "oom_end" not in st:
        # первое знакомство с блоком helper'а: неделя истории в телеграм не идет
        done = max((e["end"] for e in eps if ref - e["end"] > 900), default=0.0)
        now_apply["oom_end"] = done
    sent = _f(st.get("oom_sent"))
    sent_lvl = int(st.get("oom_sent_lvl") or 0)
    held = [str(x) for x in (st.get("oom_held") or [])]
    week = len([e for e in eps if e["start"] >= ref - 7 * 86400])
    recent = bool(sent) and now_ts - sent < REPEAT_S
    mutes = mutes or set()
    fresh = [e for e in eps if e["end"] > done]
    ready = [e for e in fresh if ref - e["end"] >= SETTLE]
    if ready:
        upd = {"oom_end": max(e["end"] for e in ready), "oom_seen": max(seen, total), "oom_wait": None}
        ready = [e for e in ready if not episode_muted(e, mutes, failed, conts)]
        if not ready:
            return None, {}, {**now_apply, **upd}
        lvl = max(level(e, failed) for e in ready)
        if recent and lvl <= sent_lvl:
            return None, {}, {**now_apply, **upd, "oom_held": held + [e["id"] for e in ready]}
        text = episode_text(ready[-1], tz, failed, week, conts, more=len(held) + len(ready) - 1,
                            late=late_text(ready[-1], now_ts))
        return (lvl, text), {**upd, "oom_held": [], "oom_sent": now_ts, "oom_sent_lvl": lvl,
                             "oom_ep": ready[-1]["id"]}, now_apply
    if held and not recent:
        # час после сообщения прошел, а отложенное не сказано: одно сообщение про все (что не
        # заглушили, пока оно ждало)
        by_id = {e["id"]: e for e in eps}
        held = [i for i in held if not ("oom" in mutes or (i in by_id and episode_muted(by_id[i], mutes, failed, conts)))]
        if not held:
            return None, {}, {**now_apply, "oom_held": []}
        last = next((by_id[i] for i in reversed(held) if i in by_id), None)
        if last is not None:
            lvl = level(last, failed)
            text = episode_text(last, tz, failed, week, conts, more=len(held) - 1,
                                late=late_text(last, now_ts, held=True))
            link = {"oom_ep": last["id"]}
        else:
            lvl = 1
            text = f"нехватка памяти (OOM) повторялась: с прошлого сообщения еще {len(held)} раз"
            link = {}
        return (lvl, text), {"oom_held": [], "oom_sent": now_ts, "oom_sent_lvl": lvl, **link}, now_apply
    if total > seen and not fresh:
        wait = _f(st.get("oom_wait"))
        if any(e["end"] >= (wait or now_ts) - 600 for e in eps):
            # убийство уже в эпизоде, о котором сказали: отчет агента пришел позже журнала
            return None, {}, {**now_apply, "oom_seen": total, "oom_wait": None}
        if b is not None and not wait:
            return None, {}, {**now_apply, "oom_wait": now_ts}
        if b is not None and now_ts - wait < WAIT_DETAILS:
            return None, {}, now_apply
        # helper'а нет (или журнал молчит): по счетчику, без подробностей
        upd = {"oom_seen": total, "oom_wait": None}
        if "oom" in mutes:
            return None, {}, {**now_apply, **upd}
        if recent:
            return None, {}, {**now_apply, **upd, "oom_held": held + [f"c{total}"]}
        victim = (s.oom_victim or "").strip()
        text = f"нехватка памяти (OOM): ядро убило {total - seen} процесс(ов)" + (f", последний {victim}" if victim else "")
        return (1, text), {**upd, "oom_held": [], "oom_sent": now_ts, "oom_sent_lvl": 1}, now_apply
    return None, {}, now_apply


def problem(s: Server, now: datetime) -> tuple[int, str, str] | None:
    """Для "Что сломано": OOM за сутки без заглушенных - из-за кого, сколько раз, когда последний и
    кого убили, и ключ мьюта по виновнику последнего ("oom", если виновника не понять). Виновник в
    тексте - то, что глушит 🔕 строки. Единичный OOM, после которого все поднялось, - событие, а не
    поломка: на главной HOME_ONE_S после него. Повторы за сутки и служба, которая после OOM лежит, -
    пока они есть."""
    mutes = mutes_now(s, now)
    if "oom" in mutes:
        return None
    rep = s.last_report or {}
    b = block(rep, now.timestamp())
    if b is None:
        return None
    failed, conts = units_failed_now(rep), containers_of(rep)
    day = [e for e in episodes(records(b)) if e["start"] >= now.timestamp() - 86400
           and not episode_muted(e, mutes, failed, conts)]
    if not day:
        return None
    if (len(day) == 1 and now.timestamp() - day[0]["end"] >= HOME_ONE_S
            and not any(down for _u, down in failed_units(day[0], failed))):
        return None
    lvl = max(level(e, failed) for e in day)
    ago = max(0.0, now.timestamp() - day[-1]["start"])
    when = f"{int(ago // 3600)} ч назад" if ago >= 3600 else f"{max(1, int(ago // 60))} мин назад"
    n = f"{len(day)} раз за сутки, последний" if len(day) > 1 else "была"
    key = culprit(day[-1], conts)
    who = f" из-за {culprit_text(key)}" if key else ""
    return (lvl, f"нехватка памяти (OOM){who}: {n} {when}, убиты {kills_text(day[-1], conts)}",
            MUTE_PREFIX + key if key else "oom")
