"""Признаки взлома в файлах сайтов (helper webfiles-setup, блок extras["webfiles"]).

Helper сам находит веб-корни (рабочие каталоги php/Apache, root из конфигов nginx и Apache,
/var/www/*, в контейнерах и подах через /proc/<pid>/root) и раз в 10 минут присылает разницу
списков кода: новые, измененные и пропавшие *.php, *.js, .htaccess, .user.ini.

Красное (признак, алерт) - только улика, у которой нет законного объяснения:
- php похож на веб-шелл: eval от base64 и подобного, вызов с $_POST, имена известных шеллов (x);
- php в загрузках: wp-content/uploads, upload(s) и tmp в корне сайта или под public/web,
  sites/*/files Drupal. Только каталоги данных: files, images, media в коде (src/Files, Upload/)
  - обычные классы, раньше они давали ложное красное при каждом обновлении;
- .htaccess или .user.ini включает php для картинок и текста, запускает php в загрузках или
  подмешивает файл ко всем запросам (auto_prepend_file из загрузок, tmp, не .php) (e);
- файл ядра или плагина WordPress не такой, как в официальном выпуске той же версии на
  wordpress.org, или лишний php в wp-admin/wp-includes. Helper присылает md5 таких файлов (это
  публичный код), эталон панель берет сама (api.wordpress.org, downloads.wordpress.org); сверка
  ждет следующего прогона: обновление, которое шло во время прогона, к нему закончится и сменит
  версию. Нет эталона (платный плагин, нет сети) - не признак.
Все остальное - серые строки "что менялось": выкладка (много файлов за прогон, в одну секунду,
через git по reflog) с тем, кто записал, правки, обновления и переводы WordPress, файлы, которые
плагин держит сам, перезапись тем же содержимым. Правка своего кода без улик от выкладки не
отличить, поэтому она не признак: так было 10 из 10 срабатываний с 21.10, все ложные.

Генерируемое в работе - не код: кэши и скомпилированные шаблоны не в счет совсем, ассеты, которые
приложение публикует само (Yii web/assets/<хеш>, Magento pub/static), - кроме php. В контейнере
helper смотрит только верхний слой overlay, ключ корня - по контейнеру, первые 10 минут после
старта только снимает список. Поды одного ворклоада - одна строка, вложенные корни одного пода
(/app и /app/admin) не дублируют признаки, один каталог в нескольких контейнерах - одна строка.

Файл, присланный измененным, helper смотрит еще 30 дней и пишет в корне "clean", когда файл
пропал, вернулся к прежнему содержимому, стал как в HEAD, - признак по нему снимается сам. Так
же снимается признак сверки, когда файл снова совпал с выпуском.

Признаки копятся в servers.webfiles, пока их не примут кнопкой ("это наше") или файл не пропадет.
Корень с доменами из конфига (или единственный сайт сервера) привязан к монитору: признаки идут
в его "Признаки взлома" (app/tamper.py). Остальные - красная проблема самого сервера.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import podnames, tg_fmt

log = logging.getLogger("kervax.webfiles")

STALE = 3 * 3600   # блок старше - helper не работает
MASS = 10          # столько файлов за прогон в одном корне - выкладка
BATCH = 5          # столько файлов с одним временем записи - их записал скрипт, не человек
GONE_ROOT = timedelta(days=7)
GONE_CT = timedelta(hours=2)  # корень контейнера, которого нет в списке, - контейнер пересоздан
GIT_BEFORE = 30   # git пишет файлы до записи в reflog: файлы за столько секунд до события - его
GIT_AFTER = 2     # и немного после: время файла и события округлено до секунды
RULES = 4          # версия правил: старые признаки один раз проходят через новые
MAX_FILES = 50     # файлов в одном признаке
WP_SPAN = 600      # обновление пишет файлы каталога подряд: разброс времени больше - не оно
WP_LANG_GAP = 2    # .l10n.php и .mo одного пакета перевода - в одну секунду (с округлением)
WP_EVENTS = 10     # обновлений WordPress в корне помним
WP_SHOW = timedelta(days=7)
WP_CHECK = 200     # файлов на сверке с wordpress.org в одном корне
WP_WAIT = timedelta(days=1)  # без эталона (нет сети) столько ждем, потом не сверяем
_PHP = (".php", ".phtml", ".phar", ".pht", ".php3", ".php4", ".php5", ".php7", ".php8")
# каталоги загрузок: только каталоги данных, а не любые files/images в коде
_UP_TOP = frozenset({"upload", "uploads", "userfiles", "tmp", "temp"})
_UP_PUB = frozenset({"public", "web", "htdocs", "pub", "public_html", "www"})
# почему файл - признак взлома
WHY = {"shell": "php похож на веб-шелл", "upload": "php в загрузках", "exec": ".htaccess запускает php",
       "wporg": "файл WordPress не как в выпуске", "wpextra": "лишний php в ядре WordPress"}
# кэши и скомпилированные шаблоны (там бывает и php) - целиком; ассеты - кроме php
_CACHE_SEGS = frozenset({".git", "node_modules", "cache", ".cache", "managed_cache", "stack_cache", "templates_c",
                         "runtime"})
_CACHE_PATHS = ("/storage/framework/", "/var/cache/", "/bootstrap/cache/", "/files/php/twig/",
                "/var/view_preprocessed/")
_ASSET_PATHS = ("/web/assets/", "/public/assets/", "/pub/static/", "/web/cpresources/")
# данные плагинов, которые лежат в php, чтобы их не отдал веб-сервер: Wordfence держит так правила
# и журнал WAF и переписывает их постоянно
_DATA_PHP = re.compile(r"(?:^|/)wp-content/wflogs/(?:attack-data|config(?:-[a-z]+)?|ips|rules|template)\.php$")


# languages/[plugins|themes/]<slug>-<locale>.l10n.php, у ядра без slug: ru_RU, admin-ru_RU
# вердикт helper'а 0.7: файл держит сам плагин (drop-in по шаблону), конфиг без кода, закрытый .htaccess
_GEN = re.compile(r"^(?:plugin:[A-Za-z0-9._-]{1,60}|data|deny)$")
_WP_LANG = re.compile(r"(?:^|/)languages/(?:(plugins|themes)/)?([^/]+)\.l10n\.php$")
_WP_LOCALE = re.compile(r"^(?:(.+)-)?([a-z]{2,3}(?:_[A-Z]{2})?(?:_[a-z0-9]+)?)$")
_WP_PART = re.compile(r"(?:^|/)wp-content/(plugins|themes)/([^/]+?)(?:\.php)?(?:/|$)")
_WP_CORE = re.compile(r"(?:^|/)wp-(?:admin|includes)/")
# файлы ядра в корне WordPress; wp-config.php обновление не пишет
_WP_CORE_TOP = re.compile(r"^(?:index\.php|xmlrpc\.php|wp-(?!config\.php)[^/]+\.php)$")
# сверка: ссылка helper'а 0.8 на выпуск, версия и путь внутри него
_REF = re.compile(r"^(?:core|plugin:[a-z0-9._-]{1,80})$")
_VER = re.compile(r"^[A-Za-z0-9._+-]{1,30}$")
_MD5 = re.compile(r"^[0-9a-f]{32}$")


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
    if _DATA_PHP.search(p):
        return True
    return not is_php(p) and any(a in full for a in _ASSET_PATHS)


def in_container(r: dict) -> bool:
    return (r.get("where") or "") not in ("", "host")


def in_uploads(p: str) -> bool:
    """Файл в каталоге загрузок: wp-content/uploads, upload(s)/tmp в корне сайта или прямо под
    public/web (Yii: backend/web/uploads), sites/<сайт>/files Drupal. С учетом регистра: классы
    (src/Web/Upload) пишут с большой буквы. Как UP_AWK в helper'е."""
    s = p.split("/")[:-1]
    if "wp-content" in s and "uploads" in s[s.index("wp-content") + 1:]:
        return True
    if len(s) >= 3 and s[0] == "sites" and s[2] == "files":
        return True
    return any(x in _UP_TOP and (i == 0 or s[i - 1] in _UP_PUB) for i, x in enumerate(s))


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
    # новое состояние сразу по текущим правилам, старое один раз проходит через _rules
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
        for k, v in (("src", str(r.get("src") or "")), ("git", _events(r.get("git"))),
                     ("wp", str(r.get("wp") or "")), ("branch", str(r.get("branch") or "")),
                     ("wploc", str(r.get("wploc") or ""))):
            if v:
                cur[k] = v
            else:
                cur.pop(k, None)
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
    # досмотр helper'а: пропал, вернулся к прежнему, как в HEAD, пакет перевода
    for r in b.get("roots") or []:
        if isinstance(r, dict) and r.get("key") and r.get("clean"):
            _clean(roots.get(str(r["key"])) or {}, r["clean"], now)
    if int(st["r"]) < RULES:
        _rules(st, int(st["r"]))
        st["r"] = RULES
    for r in roots.values():
        _git_clean(r)
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


def strong(p: str, v: dict) -> str:
    """Почему файл - улика (ключ WHY), пусто - не улика. Для признаков, набранных до правил 4,
    у которых нет причины: веб-шелл по x, php в загрузках по пути."""
    if v.get("y") in WHY:
        return v["y"]
    if v.get("x"):
        return "shell"
    if is_php(p) and in_uploads(p) and not _quiet_index(p, int(v.get("s", -1))):
        return "upload"
    return ""


def _rules(st: dict, was: int) -> None:
    """Признаки, набранные по старым правилам, проходят через новые. С правил 4 признак - только
    улика: правки ("edit"), новые php вне загрузок и выбор "здесь правят руками" уходят."""
    st.pop("modes", None)
    for r in (st.get("roots") or {}).values():
        r.pop("edit", None)
        php = r.get("php") or {}
        for p in [p for p in php if generated(p) or not strong(p, php[p])]:
            php.pop(p)
        for p, v in php.items():
            v["y"] = strong(p, v)
        if "php" in r and not php:
            r.pop("php")


CLEAN = {"gone": "файл удален", "back": "файл вернули как было", "head": "файл стал как в git (HEAD)",
         "wplang": "это пакет перевода WordPress", "plugin": "файл записал плагин по своему шаблону",
         "data": "в файле нет кода, только настройки", "deny": ".htaccess только закрывает доступ"}


def _clean(root: dict, lst, now: datetime) -> None:
    """Признаки по файлам, с которыми helper разобрался: [[путь, почему], ...]. Почему - в CLEAN, у
    плагина с его slug ("plugin:wp-rocket"). Улика снимается, только когда файл удален или вернулся
    к прежнему; файл, который стал как в git, снимает и признак сверки с WordPress."""
    for x in lst or []:
        if not (isinstance(x, (list, tuple)) and len(x) >= 2 and (str(x[1]) in CLEAN or _GEN.match(str(x[1])))):
            continue
        p, (why, _sep, slug) = str(x[0]), str(x[1]).partition(":")
        if why in ("gone", "back", "head"):
            (root.get("wpchk") or {}).pop(p, None)
        v = (root.get("php") or {}).get(p)
        if v is None or why not in ("gone", "back") and not (why == "head" and v.get("y") in ("wporg", "wpextra")):
            continue
        root["php"].pop(p)
        root["cleared"] = {"at": now.isoformat(), "p": p, "why": why, **({"slug": slug} if slug else {})}
        if not root["php"]:
            root.pop("php")
    if "wpchk" in root and not root["wpchk"]:
        root.pop("wpchk")


def _nested(roots: dict) -> None:
    """Вложенные корни одного пода (/app у php-fpm и /app/admin у nginx рядом) видят одни и те же
    файлы: признак остается у вложенного - у него домен сайта."""
    for a in roots.values():
        for b in roots.values():
            pa, pb = a.get("path") or "", b.get("path") or ""
            if a is b or a.get("where") != b.get("where") or not pb.startswith(pa.rstrip("/") + "/"):
                continue
            rel = pb[len(pa.rstrip("/")) + 1:].rstrip("/") + "/"
            for k in ("php", "wpchk"):
                dup = [p for p in a.get(k) or {} if p.startswith(rel) and p[len(rel):] in (b.get(k) or {})]
                for p in dup:
                    a[k].pop(p)
                if k in a and not a[k]:
                    a.pop(k)


def _events(lst) -> list[list]:
    """[[ts, "pull"], ...] из блока helper'а, по времени."""
    out = []
    for x in lst or []:
        if isinstance(x, (list, tuple)) and x:
            try:
                out.append([int(x[0]), str(x[1] if len(x) > 1 else "")])
            except (TypeError, ValueError):
                continue
    return sorted(out)


def _git_event(root: dict, t: int) -> list | None:
    """Событие reflog, которым записан файл: время файла за секунды до события."""
    if t <= 0:
        return None
    # ближайшее событие не раньше файла: git пишет reflog после файлов
    near = [e for e in root.get("git") or [] if e[0] - GIT_BEFORE <= t <= e[0] + GIT_AFTER]
    return min(near, key=lambda e: (e[0] < t, abs(e[0] - t)), default=None)


def _git_deploy(root: dict, ev: list, n: int, owners: list[str] | None = None) -> None:
    at = _iso(ev[0])
    old = root.get("deploy") or {}
    if old.get("at") == at and old.get("git"):
        n = max(n, int(old.get("n") or 0))
        owners = owners or old.get("o")
    elif old.get("at", "") > at:
        return
    root["deploy"] = {"at": at, "n": n, "git": ev[1], **({"o": owners} if owners else {})}


def _git_clean(root: dict) -> None:
    """Сверка с WordPress не нужна файлам, которые записала выкладка через git: reflog мог прийти
    позже файла (git дописывает его в конце)."""
    if not root.get("git"):
        return
    chk = root.get("wpchk") or {}
    for p in [p for p, v in chk.items() if _git_event(root, int(v.get("t") or 0))]:
        chk.pop(p)
    if "wpchk" in root and not chk:
        root.pop("wpchk")


def wp_part(p: str, wp: bool = False) -> tuple[str, str] | None:
    """Чей файл WordPress: ("plugin", slug), ("theme", slug), ("core", ""); None - не его.
    index.php и wp-*.php в корне - ядро, только если корень точно WordPress (wp)."""
    m = _WP_PART.search(p)
    if m:
        return m.group(1)[:-1], m.group(2)
    if _WP_CORE.search(p) or wp and "/" not in p and _WP_CORE_TOP.match(p):
        return "core", ""
    return None


def _wp_lang(p: str, t: int, mo: int | None) -> dict | None:
    """Пакет перевода, который WordPress поставил сам: .l10n.php и .mo рядом в одну секунду."""
    m = _WP_LANG.search(p)
    if not m or not mo or t <= 0 or abs(mo - t) > WP_LANG_GAP:
        return None
    loc = _WP_LOCALE.match(m.group(2))
    if not loc:
        return None
    part = (m.group(1) or "")[:-1] or "core"
    return {"k": "lang", "part": part, "slug": "" if part == "core" else loc.group(1) or "",
            "loc": loc.group(2), "at": _iso(t)}


def _wp_covers(up: list[str], p: str) -> bool:
    """Файл из каталога обновленного плагина, темы или ядра (prefix - путь от корня)."""
    kind, prefix = up[0], up[4]
    if kind == "core":
        if not p.startswith(prefix):
            return False
        r = p[len(prefix):]
        return r.startswith(("wp-admin/", "wp-includes/")) or bool(_WP_CORE_TOP.match(r))
    return p.startswith(prefix) if prefix.endswith("/") else p == prefix


def _wp_ups(lst) -> list[list[str]]:
    """[[вид, slug, было, стало, путь], ...] из прогона helper'а."""
    out = []
    for x in lst or []:
        if isinstance(x, (list, tuple)) and len(x) >= 5 and x[0] in ("core", "plugin", "theme"):
            out.append([str(v) for v in x[:5]])
    return out


def _ev_key(e: dict) -> tuple:
    return e["k"], e.get("part", ""), e.get("slug", ""), e.get("loc", "")


def _wp_event(root: dict, ev: dict) -> None:
    evs = [e for e in root.get("wpev") or [] if _ev_key(e) != _ev_key(ev)]
    root["wpev"] = sorted(evs + [ev], key=lambda e: e.get("at", ""), reverse=True)[:WP_EVENTS]


def _wp_explain(root: dict, d: dict, rows: list[tuple[str, int, int]], ts: float) -> set[str]:
    """Файлы, которые записал сам WordPress: пакет перевода, обновление плагина, темы или ядра."""
    mo = {}
    for x in (d.get("new") or []) + (d.get("changed") or []):
        if isinstance(x, (list, tuple)) and len(x) >= 4 and isinstance(x[0], str):
            try:
                mo[x[0]] = int(x[3])
            except (TypeError, ValueError):
                pass
    out: set[str] = set()
    for p, t, _s in rows:
        ev = _wp_lang(p, t, mo.get(p))
        if ev:
            _wp_event(root, ev)
            out.add(p)
    for up in _wp_ups(d.get("wp")):
        mine = [(p, t) for p, t, _s in rows if p not in out and _wp_covers(up, p)]
        times = [t for _p, t in mine if t > 0]
        # файлы одного обновления пишутся подряд; правка задолго до него - не его
        if times and max(times) - min(times) <= WP_SPAN:
            out.update(p for p, _t in mine)
        _wp_event(root, {"k": up[0], "slug": up[1], "from": up[2], "to": up[3],
                         "at": _iso(min(times) if times else ts)})
    return out


def _strs(lst) -> set[str]:
    return {x for x in lst or [] if isinstance(x, str)}


def _info(d: dict) -> dict[str, dict]:
    """Что helper знает о присланных файлах: {путь: [git, владелец, веб-сервер, веб-шелл, вердикт
    0.7, запуск php 0.8]} -> {путь: {"g": ..., "o": ..., "w": 1, "x": 1, "d": "plugin:wp-rocket",
    "e": 1}}; у 0.5 - только git_dirty."""
    out: dict[str, dict] = {}
    for p, v in (d.get("f") or {}).items():
        if not (isinstance(p, str) and isinstance(v, (list, tuple))):
            continue
        g, o, w, x, gen, e = (list(v) + ["", "", 0, 0, "", 0])[:6]
        i = {"g": str(g or ""), "o": str(o or "")[:48], "w": 1 if w == 1 else 0, "x": 1 if x == 1 else 0,
             "d": str(gen) if _GEN.match(str(gen or "")) and x != 1 else "", "e": 1 if e == 1 else 0}
        out[p] = {k: v for k, v in i.items() if v}
    for p in _strs(d.get("git_dirty")):
        out.setdefault(p, {"g": "dirty"})
    for p in _strs(d.get("git_same")):
        out.setdefault(p, {"g": "same"})
    return out


def _sums(d: dict) -> dict[str, dict]:
    """Файлы ядра и плагинов WordPress для сверки (helper 0.8): {путь: [md5, ссылка, путь в выпуске,
    версия]} -> {путь: {"m", "ref", "rel", "ver"}}."""
    out = {}
    for p, v in (d.get("sums") or {}).items():
        if not (isinstance(p, str) and isinstance(v, (list, tuple)) and len(v) >= 4):
            continue
        m, ref, rel, ver = (str(x) for x in v[:4])
        if _MD5.match(m) and _REF.match(ref) and _VER.match(ver) and rel and ".." not in rel.split("/"):
            out[p] = {"m": m, "ref": ref, "rel": rel[:300], "ver": ver}
    return out


def _owners(files: list[str], info: dict[str, dict]) -> list[str]:
    """Кто записал файлы строки: самые частые владельцы, не больше двух."""
    c = Counter(info[p]["o"] for p in files if info.get(p, {}).get("o"))
    return sorted(c, key=lambda o: (-c[o], o))[:2]


def _row(rows: list[tuple[str, int, int]], info: dict[str, dict], ts: float, n: int | None = None) -> dict:
    """Серая строка о файлах: когда (первый и последний), сколько, какие, кто записал."""
    files = sorted({p for p, _t, _s in rows})
    times = [t for _p, t, _s in rows if t > 0]
    out = {"at": _iso(max(times) if times else ts), "files": files[:5], "n": max(n or 0, len(files))}
    if times and max(times) - min(times) >= 60:
        out["from"] = _iso(min(times))
    o = _owners(files, info)
    if o:
        out["o"] = o
    return out


def _same(root: dict, rows: list[tuple[str, int, int]], again: list[tuple[str, int, int]], ts: float) -> None:
    """Тихая строка "перезаписаны тем же содержимым": rows - файлы, ставшие как в HEAD, again -
    записанные заново тем же содержимым, что было."""
    if not rows and not again:
        return
    files = sorted({p for p, _t, _s in rows + again})
    times = [t for _p, t, _s in rows + again if t > 0]
    root["same"] = {"at": _iso(max(times) if times else ts), "files": files[:5], "n": len(files), "git": bool(rows)}


def _gen(root: dict, rows: list[tuple[str, int, int]], info: dict[str, dict], ts: float) -> None:
    """Тихая строка "это файлы плагина": drop-in по шаблону, конфиг без кода, закрытый .htaccess."""
    if not rows:
        return
    files = sorted({p for p, _t, _s in rows})
    times = [t for _p, t, _s in rows if t > 0]
    root["gen"] = {"at": _iso(max(times) if times else ts), "files": files[:5], "n": len(files),
                   "by": sorted({info[p]["d"] for p in files})[:5]}


def _apply(root: dict, d: dict, ts: float) -> None:
    new = [x for x in _rows(d.get("new")) if not generated(x[0])]
    changed = [x for x in _rows(d.get("changed")) if not generated(x[0])]
    info, sums = _info(d), _sums(d)

    def why(p: str, size: int) -> str:
        i = info.get(p, {})
        # то, что закоммичено в git, взломом не бывает: в репозиторий злоумышленник не пишет
        if i.get("g") == "same":
            return ""
        if i.get("x") == 1 and is_php(p):
            return "shell"
        if i.get("e") == 1:
            return "exec"
        return "upload" if is_php(p) and in_uploads(p) and not _quiet_index(p, size) else ""

    rows = []
    for p, t, size in new + changed:
        y = why(p, size)
        if y:
            _add(root, p, ts, size, t, {"k": "new" if (p, t, size) in new else "chg", "y": y, **info.get(p, {})})
        else:
            rows.append((p, t, size))
    # обновление WordPress меняет версию: файлы на сверке ждут эталон новой версии
    for up in _wp_ups(d.get("wp")):
        ref = "core" if up[0] == "core" else f"plugin:{up[1]}" if up[0] == "plugin" else ""
        for v in (root.get("wpchk") or {}).values():
            if ref and v.get("ref") == ref:
                v.update(ver=up[3], prev=up[2], run=ts)
    # файлы ядра и плагинов - на сверку с выпуском на wordpress.org (обновление тоже: подмену в нем
    # видно только так)
    for p, t, _s in rows:
        if p in sums:
            _check(root, p, {**sums[p], "t": t, "run": ts}, info.get(p, {}))
    # серые строки: что WordPress обновил сам, выкладка через git, тем же содержимым, файлы плагина
    wp = _wp_explain(root, d, rows, ts)
    by_git: dict[str, list] = {}
    for p, t, _s in rows:
        ev = _git_event(root, t) if t > 0 and p not in wp else None
        if ev:
            by_git[p] = ev
    if by_git:
        last = max(by_git.values())
        mine = [p for p, e in by_git.items() if e == last]
        _git_deploy(root, last, len(mine), _owners(mine, info))
    head = [x for x in rows if info.get(x[0], {}).get("g") == "same" and x[0] not in by_git and x[0] not in wp]
    _same(root, head, [x for x in _rows(d.get("same")) if not generated(x[0])], ts)
    gen = [x for x in rows if info.get(x[0], {}).get("d") and x[0] not in by_git and x[0] not in wp]
    _gen(root, gen, info, ts)
    quiet = wp | set(by_git) | {p for p, _t, _s in head + gen}
    rest = [x for x in rows if x[0] not in quiet]
    # улики - своей строкой, в счет правок не идут
    total = int(d.get("n_new") or len(new)) + int(d.get("n_changed") or len(changed)) - (len(new) + len(changed) - len(rows))
    if quiet:
        # из списка (до 15 каждого вида) объяснено все - остальное, которого не видно, того же
        total = total - len(quiet) if rest else 0
    at = Counter(t for _p, t, _s in rest if t > 0)
    if rest and (total >= MASS or max(at.values(), default=0) >= BATCH):
        # выкладка без git: много файлов за прогон или скрипт записал их в одну секунду
        root["deploy"] = _row(rest, info, ts, total)
    elif rest:
        root["edits"] = _row(rest, info, ts, total)
    for g in d.get("gone") or []:
        p = str(g[0] if isinstance(g, (list, tuple)) and g else g)
        for k in ("php", "wpchk"):
            (root.get(k) or {}).pop(p, None)
    for k in ("php", "wpchk"):
        if k in root and not root[k]:
            root.pop(k)


def _check(root: dict, p: str, s: dict, i: dict) -> None:
    """Файл WordPress на сверку: новое содержимое сверяется заново, признак по старому ждет итога."""
    chk = root.setdefault("wpchk", {})
    if p in chk or len(chk) < WP_CHECK:
        chk[p] = {k: v for k, v in {**s, "o": i.get("o", "")}.items() if v not in ("", None)}


_FILE_KEYS = ("k", "g", "o", "w", "x", "y", "ver", "ref")


def _add(root: dict, p: str, ts: float, size: int, t: int = 0, about: dict | None = None) -> None:
    """about - что известно о файле: k (new/chg), y - почему улика (WHY), g (что знает git), o -
    владелец, w - записал пользователь веб-сервера, x - похож на веб-шелл. Время признака - первое."""
    files = root.setdefault("php", {})
    if p in files or len(files) < MAX_FILES:
        old = files.get(p) or {}
        files[p] = {"since": old.get("since") or _iso(ts), "s": size}
        if t > 0:
            files[p]["t"] = t
        about = about or {}
        # первый раз новый - так и остается
        if old.get("k") == "new":
            about = {**about, "k": "new"}
        for k in _FILE_KEYS:
            if about.get(k):
                files[p][k] = about[k]


def accept(state: dict | None, root: str = "", key: str = "", paths: list[str] | None = None) -> dict:
    """"Это наше": признаки корня (или всех) снимаются, файлы считаются своими."""
    st = json.loads(json.dumps(state or {}))
    want = set(root.split(",")) if root else set()
    for rk, r in (st.get("roots") or {}).items():
        if want and rk not in want or key and key != "php" or "php" not in r:
            continue
        if paths:
            for p in paths:
                r["php"].pop(p, None)
            if r["php"]:
                continue
        r.pop("php")
    return st


# ---------- сверка с выпуском WordPress ----------

# (ссылка, версия, локаль) -> (до какого времени верить, {путь: [md5, ...]}); {} - выпуска нет
# (платный плагин, такой версии нет), None - не загрузилось
_SUMS: dict[tuple[str, str, str], tuple[float, dict | None]] = {}
_SUMS_MAX = 60
_SUMS_OK, _SUMS_NONE, _SUMS_ERR = 7 * 86400, 86400, 600
_fetch_task: asyncio.Task | None = None


def _sums_get(ref: str, ver: str, loc: str) -> dict | None:
    """Эталон из кэша: dict - есть ({} - выпуска нет), None - еще не загружен."""
    x = _SUMS.get((ref, ver, loc if ref == "core" else ""))
    return x[1] if x is not None and x[0] >= time.time() else None


def _sums_url(ref: str, ver: str, loc: str) -> str:
    if ref == "core":
        return f"https://api.wordpress.org/core/checksums/1.0/?version={quote(ver)}&locale={quote(loc or 'en_US')}"
    return f"https://downloads.wordpress.org/plugin-checksums/{quote(ref[7:])}/{quote(ver)}.json"


def _sums_parse(ref: str, data) -> dict:
    """{путь: [md5, ...]} из ответа wordpress.org; у плагина md5 бывает списком."""
    if not isinstance(data, dict):
        return {}
    raw = data.get("checksums") if ref == "core" else data.get("files")
    out = {}
    for p, v in (raw or {}).items() if isinstance(raw, dict) else ():
        m = v.get("md5") if isinstance(v, dict) else v
        out[str(p)] = [str(x) for x in (m if isinstance(m, list) else [m]) if x]
    return out


async def _fetch(keys: list[tuple[str, str, str]]) -> None:
    import httpx

    from app import checks as checks_exec
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 headers={"User-Agent": checks_exec._USER_AGENT}) as c:  # noqa: SLF001
        for ref, ver, loc in keys:
            try:
                r = await c.get(_sums_url(ref, ver, loc))
                if r.status_code == 404:
                    val, ttl = {}, _SUMS_NONE
                elif r.status_code == 200:
                    val = _sums_parse(ref, r.json())
                    ttl = _SUMS_OK if val else _SUMS_NONE
                else:
                    val, ttl = None, _SUMS_ERR
            except (httpx.HTTPError, ValueError):
                log.info("эталон WordPress %s %s не загрузился", ref, ver, exc_info=True)
                val, ttl = None, _SUMS_ERR
            if len(_SUMS) >= _SUMS_MAX:
                _SUMS.pop(min(_SUMS, key=lambda k: _SUMS[k][0]))
            _SUMS[(ref, ver, loc)] = (time.time() + ttl, val)


def _want(roots: dict) -> list[tuple[str, str, str]]:
    """Каких эталонов не хватает файлам на сверке."""
    out: list[tuple[str, str, str]] = []
    for r in roots.values():
        for v in (r.get("wpchk") or {}).values():
            for ver in (v.get("ver"), v.get("prev")):
                key = (v["ref"], ver, (r.get("wploc") or "en_US") if v["ref"] == "core" else "")
                if ver and key not in out and _SUMS.get(key, (0, None))[0] < time.time():
                    out.append(key)
    return out


def _start_fetch(keys: list[tuple[str, str, str]]) -> None:
    """Загрузка эталонов фоном: стадия не ждет сеть, сверка возьмет их следующим тиком."""
    global _fetch_task
    if keys and (_fetch_task is None or _fetch_task.done()):
        _fetch_task = asyncio.get_running_loop().create_task(_fetch(keys[:10]))


def verify(state: dict | None, now: datetime, sums=_sums_get) -> dict:
    """Файлы на сверке против эталона wordpress.org. Совпал с выпуском этой или прошлой версии
    (обновление шло во время прогона) - не признак. Не совпал или лишний php в wp-admin/wp-includes
    - признак, если после присылки был еще прогон, а версия не менялась: обновление к нему
    закончилось бы. Эталона нет (платный плагин) - не сверяем, нет сети - ждем сутки."""
    st = json.loads(json.dumps(state or {}))
    seen = float(st.get("seen") or 0)
    for r in (st.get("roots") or {}).values():
        chk = r.get("wpchk") or {}
        for p, v in list(chk.items()):
            loc = (r.get("wploc") or "en_US") if v["ref"] == "core" else ""
            ref_sums = [sums(v["ref"], ver, loc) for ver in (v.get("ver"), v.get("prev")) if ver]
            if any(x is None for x in ref_sums):
                if now.timestamp() - float(v.get("run") or 0) > WP_WAIT.total_seconds():
                    chk.pop(p)
                continue
            have = [x for x in ref_sums if x]
            if not have:
                chk.pop(p)
                continue
            known = [x[v["rel"]] for x in have if v["rel"] in x]
            if any(v["m"] in ms for ms in known):
                chk.pop(p)
                old = (r.get("php") or {}).get(p)
                if old and old.get("y") in ("wporg", "wpextra"):
                    r["php"].pop(p)
                    r["cleared"] = {"at": now.isoformat(), "p": p, "why": "wporg"}
                continue
            core_php = v["ref"] == "core" and is_php(p) and v["rel"].startswith(("wp-admin/", "wp-includes/"))
            if not known and not core_php:
                # файла нет в выпуске плагина (кэш, свой файл рядом) - не сверить
                chk.pop(p)
                continue
            if float(v.get("run") or 0) >= seen:
                continue
            chk.pop(p)
            _add(r, p, float(v["run"]), 0, int(v.get("t") or 0),
                 {"k": "chg" if known else "new", "y": "wporg" if known else "wpextra", "o": v.get("o", ""),
                  "ver": v["ver"], "ref": v["ref"]})
        if "wpchk" in r and not chk:
            r.pop("wpchk")
        if "php" in r and not r["php"]:
            r.pop("php")
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
    """Где корень: пусто - на хосте, иначе контейнер или ворклоад пода без хеша (helper пишет под с
    неймспейсом "tech1/backend-7fc48994cb-7kqmt" -> "tech1/backend")."""
    if not in_container(root):
        return ""
    ns, _sep, name = (root.get("where") or "").rpartition("/")
    return podnames.label(ns, name)


def where(srv_name: str, root: dict) -> str:
    w = workload(root)
    return f"{srv_name}, {w}" if w else srv_name


def groups(roots: dict) -> list[tuple[str, str, list[tuple[str, dict]]]]:
    """(ворклоады, путь, корни): поды одного ворклоада с одним путем - одна строка, один каталог,
    смонтированный в несколько контейнеров (src), - тоже, с контейнерами через запятую."""
    out: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for rk, r in roots.items():
        src = r.get("src") if in_container(r) and not r.get("layer") else ""
        out.setdefault(("", "=" + src) if src else (workload(r), r.get("path") or ""), []).append((rk, r))
    rows = []
    for items in out.values():
        items = sorted(items)
        w = ", ".join(sorted({workload(r) for _rk, r in items}))
        rows.append((w, min(r.get("path") or "" for _rk, r in items), items))
    return sorted(rows, key=lambda x: (x[0] != "", x[1], x[0]))


def _merged(items: list[tuple[str, dict]]) -> dict[str, dict]:
    """Признаки строки (поды одного ворклоада): каждый файл раз, с самым ранним временем."""
    out: dict[str, dict] = {}
    for _rk, r in items:
        for p, v in (r.get("php") or {}).items():
            if p not in out or v.get("since", "") < out[p].get("since", ""):
                out[p] = v
    return out


def mirror(servers: list[tuple[int, str, dict]], checks: list[tuple[int, str, set[int]]]):
    """({check_id: {"php": sign}}, {server_id: [ключи непривязанных корней]})."""
    by_check: dict[int, dict] = {}
    loose: dict[int, list[str]] = {}
    for sid, name, st in servers:
        for rk, r in ((st or {}).get("roots") or {}).items():
            if not r.get("php"):
                continue
            own = owners(r, sid, checks)
            if not own:
                loose.setdefault(sid, []).append(rk)
                continue
            for cid in own:
                for p, v in r["php"].items():
                    sign = by_check.setdefault(cid, {}).setdefault("php", {"files": [], "since": v["since"]})
                    f = {"p": p, "sid": sid, "root": rk, "where": where(name, r), "since": v["since"],
                         **{x: v[x] for x in ("t",) + _FILE_KEYS if v.get(x)}}
                    if r.get("wp"):
                        f["wp"] = True
                    sign["files"].append(f)
                    sign["since"] = min(sign["since"], v["since"])
    return by_check, loose


# ---------- проблема сервера ----------

def _base(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def problems(state: dict | None) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ) корней без сайта (state["loose"]): улики - красным. Привязанные к
    монитору - в его "Признаках взлома"."""
    out = []
    loose = set((state or {}).get("loose") or [])
    for w, path, items in groups((state or {}).get("roots") or {}):
        items = [(rk, r) for rk, r in items if rk in loose]
        if not items:
            continue
        # ключ - ворклоад, а не под: новый под того же деплоймента не дает нового алерта
        gk = f"{w}:{path}" if w else items[0][0]
        at = f" ({w})" if w else ""
        pf = _merged(items)
        if not pf:
            continue
        php = sorted(pf)
        text = f"похоже на взлом: {php_head(pf)} в {path}{at}: " + tg_fmt.names([_base(p) for p in php])
        out.append((2, text + _owner_txt(pf), f"php:{gk}:" + ",".join(php)))
    return out


def php_head(files: dict[str, dict]) -> str:
    """Что за улика, если у всех файлов одна: веб-шелл, php в загрузках, запуск php в .htaccess,
    файл WordPress не как в выпуске (с версией)."""
    ys = {strong(p, v) for p, v in files.items()}
    if len(ys) != 1:
        return "признаки взлома в файлах"
    y = ys.pop()
    if y == "wporg":
        refs = {(v.get("ref", ""), v.get("ver", "")) for v in files.values()}
        if len(refs) == 1:
            ref, ver = refs.pop()
            what = "файлы ядра WordPress" if ref == "core" else f"файлы плагина {ref[7:]}"
            return f"{what} не как в выпуске {ver}"
    return WHY.get(y, "признаки взлома в файлах")


def _owner_txt(files: dict[str, dict]) -> str:
    """У всех файлов один владелец - кто, и записал ли их сам сайт."""
    own = {v.get("o", "") for v in files.values()}
    if len(own) != 1 or "" in own:
        return ""
    web = all(v.get("w") for v in files.values())
    return f", владелец {own.pop()}" + (" (веб-сервер)" if web else "")


def view(state: dict | None, rep: dict, now: datetime) -> dict | None:
    """Для "Сервисов": корни, сколько кода, улики с файлами и серые строки о том, что менялось."""
    b = block(rep, now)
    roots = ((state or {}).get("roots") or {})
    loose = set((state or {}).get("loose") or [])
    if b is None and not roots:
        return None
    out = []
    for w, path, items in groups(roots):
        rs = [r for _rk, r in items]
        out.append({
            # key - корни через запятую: "это наше" снимает признаки со всех подов строки
            "key": ",".join(rk for rk, _r in items), "where": w or "host", "path": path,
            # подов самого большого ворклоада строки: php-fpm и воркер одного проекта - не поды
            "pods": max(Counter(workload(r) for r in rs).values()) if w else 0,
            "domains": list(dict.fromkeys(d for r in rs for d in r.get("domains") or []))[:20],
            "files": max(r.get("files", 0) for r in rs), "big": any(r.get("big") for r in rs),
            "layer": all(r.get("layer") for r in rs),
            "php": [{"p": p, **v, "y": strong(p, v)} for p, v in sorted(_merged(items).items())],
            "site": any(rk not in loose for rk, _r in items),
            # серые строки за неделю: выкладка, правки, что WordPress обновил сам, тем же
            # содержимым, файлы плагина, признак, который снялся сам
            "deploy": _shown(rs, now, "deploy"),
            "edits": _shown(rs, now, "edits"),
            "cleared": _shown(rs, now, "cleared"),
            "wp": next((r["wp"] for r in rs if r.get("wp")), ""),
            "wp_events": _wp_shown(rs, now),
            "branch": next((r["branch"] for r in rs if r.get("branch")), ""),
            "same": _shown(rs, now, "same"),
            "gen": _shown(rs, now, "gen"),
            # файлов WordPress ждут эталона с wordpress.org
            "checking": sum(len(r.get("wpchk") or {}) for r in rs),
        })
    return {"roots": out, "ts": (b or {}).get("ts")}


def _shown(rs: list[dict], now: datetime, k: str) -> dict | None:
    last = max((r[k] for r in rs if r.get(k)), key=lambda x: x.get("at", ""), default=None)
    try:
        return last if last and now - datetime.fromisoformat(last["at"]) <= WP_SHOW else None
    except (KeyError, ValueError):
        return None


def _wp_shown(rs: list[dict], now: datetime) -> list[dict]:
    seen, out = set(), []
    for e in sorted((e for r in rs for e in r.get("wpev") or []), key=lambda e: e.get("at", ""), reverse=True):
        try:
            fresh = now - datetime.fromisoformat(e.get("at") or "") <= WP_SHOW
        except ValueError:
            fresh = False
        if fresh and _ev_key(e) not in seen:
            seen.add(_ev_key(e))
            out.append(e)
    return out


def tg_short(items) -> tuple[str, list[str]] | None:
    rows = [p for p in items or [] if isinstance(p, (tuple, list)) and len(p) >= 2 and p[0] >= 2]
    if not rows:
        return None
    text = str(rows[0][1]).removeprefix("похоже на взлом: ")
    head, _sep, files = text.partition(": ")
    # "php в загрузках в /var/www (web)": место - после последнего " в "
    what, _sep, place = head.rpartition(" в ")
    return f"Похоже на взлом: {what}", [files.split(", правки")[0], place]


# ---------- стадия планировщика ----------

async def track(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Свежие блоки helper'а -> servers.webfiles, сверка с wordpress.org; признаки привязанных
    корней -> мониторы."""
    from app import parents as parents_mod
    from app import tamper as tamper_mod
    from app.models import Check, Server
    async with session_factory() as s:
        servers = list(await s.scalars(select(Server)))
        changed = False
        want: list[tuple[str, str, str]] = []
        for srv in servers:
            b = block(srv.last_report or {}, now)
            new = process(srv.webfiles, b, now) if b is not None else srv.webfiles
            if new and any(r.get("wpchk") for r in (new.get("roots") or {}).values()):
                new = verify(new, now)
                want += [k for k in _want(new["roots"]) if k not in want]
            if new != srv.webfiles:
                srv.webfiles = new
                changed = True
        if changed:
            await s.commit()
        _start_fetch(want)
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
            want_c = by_check.get(c.id) or {}
            st = c.tamper_state or {}
            signs = dict(st.get("signs") or {})
            before = {k: signs.get(k) for k in ("php", "edit")}
            # "edit" (правки) - признак до правил 4: снимается
            signs.pop("edit", None)
            # "отложить" в карточке сайта прячет файлы, что были в признаке, новые видно сразу
            files = [f for f in (want_c.get("php") or {}).get("files") or []
                     if not tamper_mod.hidden(st.get("post"), "php", f["p"], now)]
            if files:
                signs["php"] = {"files": files, "since": min(f["since"] for f in files)}
            else:
                signs.pop("php", None)
            if before != {k: signs.get(k) for k in ("php", "edit")}:
                c.tamper_state = {**st, "signs": signs}
                dirty = True
        if dirty:
            await s.commit()
