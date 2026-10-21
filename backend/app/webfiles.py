"""Признаки взлома в файлах сайтов (helper webfiles-setup 0.2, блок extras["webfiles"]).

Helper сам находит веб-корни (рабочие каталоги php/Apache, root из конфигов nginx и Apache,
/var/www/*, в контейнерах и подах через /proc/<pid>/root) и раз в 10 минут присылает разницу
списков кода: новые, измененные и пропавшие *.php, *.js, .htaccess. Здесь из разницы
получаются признаки:
- новые php: php в загрузках (uploads, files, images...) - всегда, в том числе в выкладке;
  новый php вне выкладки - тоже. Сильный признак;
- код изменен вне выкладки: правка или новый js/.htaccess, когда за прогон в корне поменялось
  меньше 10 файлов. Слабый признак.
Выкладка - 10 и больше файлов за прогон или 5 и больше записанных в одну секунду (скрипт:
выкладка, старт контейнера): серая строка "выкладка", не признак.

Генерируемое в работе - не код: кэши и скомпилированные шаблоны не в счет совсем, ассеты, которые
приложение публикует само (Yii web/assets/<хеш>, Magento pub/static), - кроме php. В контейнере
helper 0.2 смотрит только верхний слой overlay (записанное в живом контейнере), ключ корня - по
контейнеру, первые 10 минут после старта только снимает список. Поды одного ворклоада - одна
строка и одна проблема ("backend", подов: 5), вложенные корни одного пода (/app и /app/admin) не
дублируют признаки.

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

from app import podnames, tg_fmt

log = logging.getLogger("kervax.webfiles")

STALE = 3 * 3600   # блок старше - helper не работает
MASS = 10          # столько файлов за прогон в одном корне - выкладка
BATCH = 5          # столько файлов с одним временем записи - их записал скрипт, не человек
GONE_ROOT = timedelta(days=7)
GONE_CT = timedelta(hours=2)  # корень контейнера, которого нет в списке, - контейнер пересоздан
RULES = 2          # версия правил: старые признаки один раз проходят через новые
MAX_FILES = 50     # файлов в одном признаке
_PHP = (".php", ".phtml", ".phar", ".pht", ".php3", ".php4", ".php5", ".php7", ".php8")
_UPLOADS = frozenset({"uploads", "upload", "files", "media", "images", "img", "pictures", "userfiles",
                      "tmp", "temp"})
STRONG = frozenset({"php"})
# кэши и скомпилированные шаблоны (там бывает и php) - целиком; ассеты - кроме php
_CACHE_SEGS = frozenset({".git", "node_modules", "cache", ".cache", "managed_cache", "stack_cache", "templates_c",
                         "runtime"})
_CACHE_PATHS = ("/storage/framework/", "/var/cache/", "/bootstrap/cache/", "/files/php/twig/",
                "/var/view_preprocessed/")
_ASSET_PATHS = ("/web/assets/", "/public/assets/", "/pub/static/", "/web/cpresources/")


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


def generated(p: str) -> bool:
    """Файл пишет само приложение в работе: кэш, скомпилированный шаблон, опубликованный ассет."""
    # с учетом регистра, как find в helper'е: app/Cache/Repository.php - код, а не кэш
    full = "/" + p
    if any(seg in _CACHE_SEGS for seg in full.split("/")[1:-1]) or any(c in full for c in _CACHE_PATHS):
        return True
    return not is_php(p) and any(a in full for a in _ASSET_PATHS)


def in_container(r: dict) -> bool:
    return (r.get("where") or "") not in ("", "host")


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
    # новое состояние сразу по текущим правилам, старое (без "r") один раз проходит через _rules2
    st.setdefault("r", 1 if st.get("roots") else RULES)
    roots: dict = st.setdefault("roots", {})
    seen = float(st.get("seen") or 0)
    for r in b.get("roots") or []:
        if not isinstance(r, dict) or not r.get("key"):
            continue
        cur = roots.setdefault(str(r["key"]), {})
        cur.update(where=str(r.get("where") or ""), path=str(r.get("path") or ""),
                   domains=[str(d).lower() for d in r.get("domains") or []][:20],
                   files=int(r.get("files") or 0), big=bool(r.get("big")), layer=bool(r.get("layer")),
                   at=now.isoformat())
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
    if int(st["r"]) < RULES:
        _rules2(roots)
        st["r"] = RULES
    _nested(roots)
    for k in [k for k, r in roots.items() if _old(r, now)]:
        roots.pop(k)
    st["seen"] = seen
    return st


def _old(r: dict, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(r.get("at") or "") > (GONE_CT if in_container(r) else GONE_ROOT)
    except ValueError:
        return True


def _rules2(roots: dict) -> None:
    """Признаки, набранные по правилам helper 0.1: сгенерированное уходит, правки в контейнерах -
    тоже: 0.1 сравнивал новый под со списком старого (ключ корня переходил к нему) и считал
    правкой то, что под пишет при старте."""
    for r in roots.values():
        if in_container(r):
            r.pop("edit", None)
        for k in ("php", "edit"):
            for p in [p for p in r.get(k) or {} if generated(p)]:
                r[k].pop(p)
            if k in r and not r[k]:
                r.pop(k)


def _nested(roots: dict) -> None:
    """Вложенные корни одного пода (/app у php-fpm и /app/admin у nginx рядом) видят одни и те же
    файлы: признак остается у вложенного - у него домен сайта."""
    for a in roots.values():
        for b in roots.values():
            pa, pb = a.get("path") or "", b.get("path") or ""
            if a is b or a.get("where") != b.get("where") or not pb.startswith(pa.rstrip("/") + "/"):
                continue
            rel = pb[len(pa.rstrip("/")) + 1:].rstrip("/") + "/"
            for k in ("php", "edit"):
                dup = [p for p in a.get(k) or {} if p.startswith(rel) and p[len(rel):] in (b.get(k) or {})]
                for p in dup:
                    a[k].pop(p)
                if k in a and not a[k]:
                    a.pop(k)


def _apply(root: dict, d: dict, ts: float) -> None:
    new = [x for x in _rows(d.get("new")) if not generated(x[0])]
    changed = [x for x in _rows(d.get("changed")) if not generated(x[0])]
    gone = d.get("gone") or []
    total = int(d.get("n_new") or len(new)) + int(d.get("n_changed") or len(changed))
    same: dict[int, int] = {}
    for _p, t, _s in new + changed:
        if t > 0:
            same[t] = same.get(t, 0) + 1
    deploy = total >= MASS or max(same.values(), default=0) >= BATCH
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
    want = set(root.split(",")) if root else set()
    for rk, r in (st.get("roots") or {}).items():
        if want and rk not in want:
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


def workload(root: dict) -> str:
    """Где корень: пусто - на хосте, иначе контейнер или ворклоад пода без хеша (helper 0.2 пишет
    под с неймспейсом "tech1/backend-7fc48994cb-7kqmt" -> "tech1/backend")."""
    if not in_container(root):
        return ""
    ns, _sep, name = (root.get("where") or "").rpartition("/")
    return podnames.label(ns, name)


def where(srv_name: str, root: dict) -> str:
    w = workload(root)
    return f"{srv_name}, {w}" if w else srv_name


def groups(roots: dict) -> list[tuple[str, str, list[tuple[str, dict]]]]:
    """(ворклоад, путь, корни): поды одного ворклоада с одним путем - одна строка."""
    out: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for rk, r in roots.items():
        out.setdefault((workload(r), r.get("path") or ""), []).append((rk, r))
    return [(w, p, sorted(items)) for (w, p), items in sorted(out.items(), key=lambda x: (x[0][0] != "", x[0][1], x[0][0]))]


def _merged(items: list[tuple[str, dict]], k: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for _rk, r in items:
        for p, v in (r.get(k) or {}).items():
            if p not in out or v.get("since", "") < out[p].get("since", ""):
                out[p] = v
    return out


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
    for w, path, items in groups((state or {}).get("roots") or {}):
        items = [(rk, r) for rk, r in items if rk in loose]
        if not items:
            continue
        # ключ - ворклоад, а не под: новый под того же деплоймента не дает нового алерта
        gk = f"{w}:{path}" if w else items[0][0]
        at = f" ({w})" if w else ""
        php, edit = sorted(_merged(items, "php")), sorted(_merged(items, "edit"))
        if php:
            text = f"похоже на взлом: новые php в {path}{at}: " + tg_fmt.names([_base(p) for p in php])
            if edit:
                text += ", правки вне выкладки: " + tg_fmt.names([_base(p) for p in edit], 2)
            out.append((2, text, f"php:{gk}:" + ",".join(php)))
        elif edit:
            out.append((1, f"код изменен вне выкладки в {path}{at}: "
                           + tg_fmt.names([_base(p) for p in edit]), f"edit:{gk}:" + ",".join(edit)))
    return out


def view(state: dict | None, rep: dict, now: datetime) -> dict | None:
    """Для "Сервисов": корни, сколько кода, последняя выкладка, признаки с файлами."""
    b = block(rep, now)
    roots = ((state or {}).get("roots") or {})
    loose = set((state or {}).get("loose") or [])
    if b is None and not roots:
        return None
    out = []
    for w, path, items in groups(roots):
        rs = [r for _rk, r in items]
        deploys = [r["deploy"] for r in rs if r.get("deploy")]
        out.append({
            # key - корни через запятую: "это наше" снимает признаки со всех подов строки
            "key": ",".join(rk for rk, _r in items), "where": w or "host", "path": path,
            "pods": len(items) if w else 0,
            "domains": list(dict.fromkeys(d for r in rs for d in r.get("domains") or []))[:20],
            "files": max(r.get("files", 0) for r in rs), "big": any(r.get("big") for r in rs),
            "layer": all(r.get("layer") for r in rs),
            "deploy": max(deploys, key=lambda x: x.get("at", "")) if deploys else None,
            "php": [{"p": p, **v} for p, v in sorted(_merged(items, "php").items())],
            "edit": [{"p": p, **v} for p, v in sorted(_merged(items, "edit").items())],
            "site": any(rk not in loose for rk, _r in items),
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

