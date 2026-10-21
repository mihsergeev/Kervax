"""Признаки взлома в файлах сайтов (helper webfiles-setup 0.1, блок extras["webfiles"]).

Helper сам находит веб-корни (рабочие каталоги php/Apache, root из конфигов nginx и Apache,
/var/www/*, в контейнерах и подах через /proc/<pid>/root) и раз в 10 минут присылает разницу
списков кода: новые, измененные и пропавшие *.php, *.js, .htaccess. Здесь из разницы
получаются признаки:
- новые php: php в загрузках (uploads, files, images...) - всегда, в том числе в выкладке;
  новый php вне выкладки - тоже. Сильный признак;
- код изменен вне выкладки: правка или новый js/.htaccess, когда за прогон в корне поменялось
  меньше 10 файлов. Слабый признак.
Выкладка - 10 и больше файлов за прогон: серая строка "выкладка", не признак.

Признаки копятся в servers.webfiles, пока их не примут кнопкой ("это наше") или файл не пропадет.
Корень с доменами из конфига (или единственный сайт сервера) привязан к монитору: признаки идут
в его "Признаки взлома" и считаются вместе с признаками страницы (app/tamper.py). Остальные -
проблема самого сервера: красная при новых php (алерт), желтая при одной правке.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import tg_fmt

log = logging.getLogger("kervax.webfiles")

STALE = 3 * 3600   # блок старше - helper не работает
MASS = 10          # столько файлов за прогон в одном корне - выкладка
GONE_ROOT = timedelta(days=7)
MAX_FILES = 50     # файлов в одном признаке
_PHP = (".php", ".phtml", ".phar", ".pht", ".php3", ".php4", ".php5", ".php7", ".php8")
_UPLOADS = frozenset({"uploads", "upload", "files", "media", "images", "img", "pictures", "userfiles",
                      "tmp", "temp"})
STRONG = frozenset({"php"})


def block(rep: dict, now: datetime) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("webfiles")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("roots"), list):
        return None
    try:
        if now.timestamp() - float(b.get("ts") or 0) > STALE:
            return None
    except (TypeError, ValueError):
        return None
    return b


def is_php(p: str) -> bool:
    return p.lower().endswith(_PHP)


def in_uploads(p: str) -> bool:
    return any(seg.lower() in _UPLOADS for seg in p.split("/")[:-1])


def _quiet_index(p: str, size: int) -> bool:
    """index.php "silence is golden" в каталогах загрузок кладут сами плагины."""
    return p.rsplit("/", 1)[-1].lower() == "index.php" and 0 <= size <= 100


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _rows(lst) -> list[tuple[str, int, int]]:
    out = []
    for x in lst or []:
        if isinstance(x, (list, tuple)) and x and isinstance(x[0], str):
            try:
                out.append((x[0], int(x[1] if len(x) > 1 else 0), int(x[2] if len(x) > 2 else 0)))
            except (TypeError, ValueError):
                out.append((x[0], 0, 0))
    return out


def process(state: dict | None, b: dict, now: datetime) -> dict:
    """Новое состояние сервера по блоку helper'а: каждый прогон берем один раз (по ts)."""
    st = json.loads(json.dumps(state or {}))
    roots: dict = st.setdefault("roots", {})
    seen = float(st.get("seen") or 0)
    for r in b.get("roots") or []:
        if not isinstance(r, dict) or not r.get("key"):
            continue
        cur = roots.setdefault(str(r["key"]), {})
        cur.update(where=str(r.get("where") or ""), path=str(r.get("path") or ""),
                   domains=[str(d).lower() for d in r.get("domains") or []][:20],
                   files=int(r.get("files") or 0), big=bool(r.get("big")), at=now.isoformat())
    runs = sorted((x for x in b.get("runs") or [] if isinstance(x, dict)), key=lambda x: float(x.get("ts") or 0))
    for run in runs:
        ts = float(run.get("ts") or 0)
        if ts <= seen:
            continue
        seen = ts
        for d in run.get("roots") or []:
            if not isinstance(d, dict) or not d.get("key") or d.get("first"):
                continue
            _apply(roots.setdefault(str(d["key"]), {}), d, ts)
    for k in [k for k, r in roots.items() if _old(r, now)]:
        roots.pop(k)
    st["seen"] = seen
    return st


def _old(r: dict, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(r.get("at") or "") > GONE_ROOT
    except ValueError:
        return True


def _apply(root: dict, d: dict, ts: float) -> None:
    new, changed, gone = _rows(d.get("new")), _rows(d.get("changed")), d.get("gone") or []
    total = int(d.get("n_new") or len(new)) + int(d.get("n_changed") or len(changed))
    deploy = total >= MASS
    if deploy:
        root["deploy"] = {"at": _iso(ts), "n": total}
    since = _iso(ts)
    for p, _t, size in new:
        if is_php(p) and in_uploads(p) and not _quiet_index(p, size):
            _add(root, "php", p, since, size)
        elif deploy:
            continue
        elif is_php(p):
            _add(root, "php", p, since, size)
        else:
            _add(root, "edit", p, since, size)
    for p, _t, size in changed:
        if is_php(p) and in_uploads(p) and not _quiet_index(p, size):
            _add(root, "php", p, since, size)
        elif not deploy:
            _add(root, "edit", p, since, size)
    for g in gone:
        p = g[0] if isinstance(g, (list, tuple)) and g else g
        for k in ("php", "edit"):
            (root.get(k) or {}).pop(str(p), None)
    for k in ("php", "edit"):
        if k in root and not root[k]:
            root.pop(k)


def _add(root: dict, key: str, p: str, since: str, size: int) -> None:
    files = root.setdefault(key, {})
    if p in files or len(files) < MAX_FILES:
        files[p] = {"since": (files.get(p) or {}).get("since") or since, "s": size}


def accept(state: dict | None, root: str = "", key: str = "", paths: list[str] | None = None) -> dict:
    """"Это наше": признаки корня (или всех) снимаются, файлы считаются своими."""
    st = json.loads(json.dumps(state or {}))
    for rk, r in (st.get("roots") or {}).items():
        if root and rk != root:
            continue
        for k in ("php", "edit"):
            if key and k != key or k not in r:
                continue
            if paths:
                for p in paths:
                    r[k].pop(p, None)
                if r[k]:
                    continue
            r.pop(k)
    return st


# ---------- привязка к сайтам ----------

def _host(target: str) -> str:
    raw = (target or "").strip()
    return (urlparse(raw if "://" in raw else "https://" + raw).hostname or "").lower()


def _bare(h: str) -> str:
    return h[4:] if h.startswith("www.") else h


def _dom_match(host: str, domains: list[str]) -> bool:
    h = _bare(host)
    for d in domains:
        d = d.lower()
        if d.startswith("*.") and (h == d[2:] or h.endswith(d[1:])):
            return True
        if _bare(d) == h:
            return True
    return False


def owners(root: dict, sid: int, checks: list[tuple[int, str, set[int]]]) -> list[int]:
    """Мониторы, чей это корень: по доменам из конфига; без доменов - единственный сайт сервера."""
    doms = root.get("domains") or []
    if doms:
        return [cid for cid, host, _p in checks if host and _dom_match(host, doms)]
    mine = [cid for cid, _h, parents in checks if sid in parents]
    return mine if len(mine) == 1 else []


def where(srv_name: str, root: dict) -> str:
    w = root.get("where") or ""
    return srv_name if w in ("", "host") else f"{srv_name}, {w}"


def mirror(servers: list[tuple[int, str, dict]], checks: list[tuple[int, str, set[int]]]):
    """({check_id: {"php": sign, "edit": sign}}, {server_id: [ключи непривязанных корней]})."""
    by_check: dict[int, dict] = {}
    loose: dict[int, list[str]] = {}
    for sid, name, st in servers:
        for rk, r in ((st or {}).get("roots") or {}).items():
            if not (r.get("php") or r.get("edit")):
                continue
            own = owners(r, sid, checks)
            if not own:
                loose.setdefault(sid, []).append(rk)
                continue
            for cid in own:
                for k in ("php", "edit"):
                    for p, v in (r.get(k) or {}).items():
                        sign = by_check.setdefault(cid, {}).setdefault(k, {"files": [], "since": v["since"]})
                        sign["files"].append({"p": p, "sid": sid, "root": rk, "where": where(name, r),
                                              "since": v["since"]})
                        sign["since"] = min(sign["since"], v["since"])
    return by_check, loose


# ---------- проблема сервера ----------

def _base(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def problems(state: dict | None) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ) корней без сайта (state["loose"]): новые php - похоже на взлом,
    правка - желтым. Привязанные к монитору - в его "Признаках взлома"."""
    out = []
    loose = set((state or {}).get("loose") or [])
    for rk, r in ((state or {}).get("roots") or {}).items():
        if rk not in loose:
            continue
        w = r.get("where") or ""
        at = "" if w in ("", "host") else f" ({w})"
        php, edit = sorted(r.get("php") or {}), sorted(r.get("edit") or {})
        if php:
            text = f"похоже на взлом: новые php в {r.get('path', '')}{at}: " + tg_fmt.names([_base(p) for p in php])
            if edit:
                text += ", правки вне выкладки: " + tg_fmt.names([_base(p) for p in edit], 2)
            out.append((2, text, f"php:{rk}:" + ",".join(php)))
        elif edit:
            out.append((1, f"код изменен вне выкладки в {r.get('path', '')}{at}: "
                           + tg_fmt.names([_base(p) for p in edit]), f"edit:{rk}:" + ",".join(edit)))
    return out


def view(state: dict | None, rep: dict, now: datetime) -> dict | None:
    """Для "Сервисов": корни, сколько кода, последняя выкладка, признаки с файлами."""
    b = block(rep, now)
    roots = ((state or {}).get("roots") or {})
    loose = set((state or {}).get("loose") or [])
    if b is None and not roots:
        return None
    out = []
    for rk, r in sorted(roots.items(), key=lambda x: (x[1].get("where") != "host", x[1].get("path", ""))):
        out.append({
            "key": rk, "where": r.get("where", ""), "path": r.get("path", ""), "domains": r.get("domains") or [],
            "files": r.get("files", 0), "big": bool(r.get("big")), "deploy": r.get("deploy"),
            "php": [{"p": p, **v} for p, v in sorted((r.get("php") or {}).items())],
            "edit": [{"p": p, **v} for p, v in sorted((r.get("edit") or {}).items())],
            "site": rk not in loose,
        })
    return {"roots": out, "ts": (b or {}).get("ts")}


def tg_short(items) -> tuple[str, list[str]] | None:
    rows = [p for p in items or [] if isinstance(p, (tuple, list)) and len(p) >= 2 and p[0] >= 2]
    if not rows:
        return None
    text = str(rows[0][1]).removeprefix("похоже на взлом: ")
    head, _sep, files = text.partition(": ")
    return "Похоже на взлом: новые php", [files.split(", правки")[0], head.removeprefix("новые php в ")]


# ---------- стадия планировщика ----------

async def track(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Свежие блоки helper'а -> servers.webfiles; признаки привязанных корней -> мониторы."""
    from app import parents as parents_mod
    from app.models import Check, Server
    async with session_factory() as s:
        servers = list(await s.scalars(select(Server)))
        changed = False
        for srv in servers:
            b = block(srv.last_report or {}, now)
            if b is None:
                continue
            new = process(srv.webfiles, b, now)
            if new != srv.webfiles:
                srv.webfiles = new
                changed = True
        if changed:
            await s.commit()
        have = [(x.id, x.name, x.webfiles) for x in servers if x.webfiles]
        checks = [c for c in await s.scalars(select(Check).where(Check.type == "http"))]
        pidx = parents_mod.Index(await parents_mod.load_servers(s))
        cl = [(c.id, _host(c.target), {pid for pid, _w in parents_mod.parents_of(c, pidx)}) for c in checks]
        by_check, loose = mirror(have, cl)
        dirty = False
        for srv in servers:
            lk = sorted(loose.get(srv.id, []))
            if srv.webfiles and (srv.webfiles.get("loose") or []) != lk:
                srv.webfiles = {**srv.webfiles, "loose": lk}
                dirty = True
        for c in checks:
            want = by_check.get(c.id) or {}
            st = c.tamper_state or {}
            signs = dict(st.get("signs") or {})
            before = {k: signs.get(k) for k in ("php", "edit")}
            for k in ("php", "edit"):
                if k in want:
                    signs[k] = want[k]
                else:
                    signs.pop(k, None)
            if before != {k: signs.get(k) for k in ("php", "edit")}:
                c.tamper_state = {**st, "signs": signs}
                dirty = True
        if dirty:
            await s.commit()

