"""Признаки взлома в файлах сайтов (helper webfiles-setup, блок extras["webfiles"]).

Helper сам находит веб-корни (рабочие каталоги php/Apache, root из конфигов nginx и Apache,
/var/www/*, в контейнерах и подах через /proc/<pid>/root) и раз в 10 минут присылает разницу
списков кода: новые, измененные и пропавшие *.php, *.js, .htaccess. Здесь из разницы
получаются признаки:
- новые php: php в загрузках (uploads, files, images...) - всегда, в том числе в выкладке;
  новый php вне выкладки - тоже. Сильный признак;
- код изменен вне выкладки: правка или новый js/.htaccess, когда за прогон в корне поменялось
  меньше 10 файлов. Слабый признак.
Выкладка - 10 и больше файлов за прогон или 5 и больше записанных в одну секунду (скрипт:
выкладка, старт контейнера): серая строка "выкладка", не признак. Выкладка через git (pull
двух файлов) - по reflog: helper 0.3 присылает времена последних pull/checkout/reset репозитория,
в котором лежит корень, и файлы, записанные за секунды до такого события, - выкладка при любом
их числе. Правка в другое время - признак, как раньше; со старым helper'ом (без reflog) - тоже.

Генерируемое в работе - не код: кэши и скомпилированные шаблоны не в счет совсем, ассеты, которые
приложение публикует само (Yii web/assets/<хеш>, Magento pub/static), - кроме php. В контейнере
helper 0.2 смотрит только верхний слой overlay (записанное в живом контейнере), ключ корня - по
контейнеру, первые 10 минут после старта только снимает список. Поды одного ворклоада - одна
строка и одна проблема ("backend", подов: 5), вложенные корни одного пода (/app и /app/admin) не
дублируют признаки. Один каталог, смонтированный в несколько контейнеров (php-fpm и воркер одного
проекта), - тоже одна строка: helper 0.3 присылает устройство и inode корня.

Штатные обновления WordPress (из админки или wp-cron) - не правка. Пакет перевода пишет .l10n.php
вместе с .mo в одну секунду: helper 0.4 присылает к измененному languages/*.l10n.php время его .mo.
Плагин, тема и ядро - по смене версии: helper 0.4 присылает в прогоне, чьи версии сменились с
прошлого (Version: из заголовка, $wp_version ядра), и файлы этого каталога, записанные вместе,
- обновление. Такое показывается серой строкой ("обновлен плагин X 1.0 -> 1.1"), не признаком.
Файлы плагина или ядра изменились, а версия нет - признак, с названием, чей это файл. php в
загрузках - признак всегда.

Файл рабочей копии git helper 0.5 сверяет с HEAD: то же содержимое (IDE или инструмент в
контейнере записали файл заново) - не правка, тихая строка "перезаписан тем же содержимым".
Другое содержимое - признак, с текстом "не закоммиченная правка в рабочей копии git (ветка X)".
Файл, которого нет в HEAD, и php в загрузках - признаки, как раньше.

Helper 0.6 смотрит на содержимое, а не на время файла: у каждого файла в списке есть sha1, и
перезапись тем же содержимым приходит строкой "same", не правкой. К каждому присланному файлу -
что о нем знает git (same - как в HEAD, dirty - не как в HEAD, new - git его не знает, ignored - в
.gitignore), владелец, записал ли его пользователь веб-сервера (w) и похож ли php на веб-шелл (x).
Признак висит, только пока он правда есть: файл, который helper прислал измененным, он смотрит еще
30 дней и пишет в корне "clean", когда файл пропал, вернулся к прежнему содержимому, стал как в
HEAD (закоммитили, выложили) или оказался пакетом перевода, - признак по нему снимается сам.

Файлы, которые плагин WordPress держит сам, - не правка. Helper 0.7 присылает к файлу вердикт (d):
plugin:<slug> - drop-in в wp-content (advanced-cache.php, object-cache.php, autoptimize_404_handler.php,
wp-cache-config.php) сделан из файла этого установленного плагина: строки кода те же и в том же
порядке, заполнены только места под путь и версию, поменялись только значения настроек; data - php
в wp-content, который ничего не выполняет (exit в начале или одни значения: конфиги плагинов кэша);
deny - .htaccess, который только закрывает доступ. Такое - тихая строка, висящий признак по такому
файлу снимается сам (досмотр присылает тот же вердикт в "clean"). Подмененный drop-in (лишняя строка
кода, путь вне сайта) вердикта не получает и остается признаком, похожее на веб-шелл - тоже.

Где правят руками (рабочая копия разработчика на dev-сервере: ветка не main/master/prod/release,
или так выбрано в строке корня), правки файлов не признак, они видны серой строкой. Признаками
остаются php в загрузках, php, похожий на веб-шелл, и файлы, которые записал сам сайт (владелец -
пользователь веб-сервера или php): так попадает внутрь взлом.

Признаки копятся в servers.webfiles, пока их не примут кнопкой ("это наше") или файл не пропадет.
Корень с доменами из конфига (или единственный сайт сервера) привязан к монитору: признаки идут
в его "Признаки взлома" и считаются вместе с признаками страницы (app/tamper.py). Остальные -
проблема самого сервера: красная при новых php (алерт), желтая при одной правке.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
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
GIT_BEFORE = 30   # git пишет файлы до записи в reflog: файлы за столько секунд до события - его
GIT_AFTER = 2     # и немного после: время файла и события округлено до секунды
RUN_WIN = 660     # признак без времени файла (набран до helper'а 0.3) - событие за прогон до него
RULES = 3          # версия правил: старые признаки один раз проходят через новые
MAX_FILES = 50     # файлов в одном признаке
WP_SPAN = 600      # обновление пишет файлы каталога подряд: разброс времени больше - не оно
WP_LANG_GAP = 2    # .l10n.php и .mo одного пакета перевода - в одну секунду (с округлением)
WP_EVENTS = 10     # обновлений WordPress в корне помним
WP_SHOW = timedelta(days=7)
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
# данные плагинов, которые лежат в php, чтобы их не отдал веб-сервер: Wordfence держит так правила
# и журнал WAF и переписывает их постоянно. Только эти имена: чужой php рядом - признак
_DATA_PHP = re.compile(r"(?:^|/)wp-content/wflogs/(?:attack-data|config(?:-[a-z]+)?|ips|rules|template)\.php$")
# ветки выкладки: рабочая копия на другой ветке - копия разработчика, ее правят руками
_PROD_BRANCH = re.compile(r"^(?:main|master|prod|production|release|releases|stable|live|deploy)(?:[/._-].*)?$")
MODES = ("auto", "dev", "strict")


# languages/[plugins|themes/]<slug>-<locale>.l10n.php, у ядра без slug: ru_RU, admin-ru_RU
# вердикт helper'а 0.7: файл держит сам плагин (drop-in по шаблону), конфиг без кода, закрытый .htaccess
_GEN = re.compile(r"^(?:plugin:[A-Za-z0-9._-]{1,60}|data|deny)$")
_WP_LANG = re.compile(r"(?:^|/)languages/(?:(plugins|themes)/)?([^/]+)\.l10n\.php$")
_WP_LOCALE = re.compile(r"^(?:(.+)-)?([a-z]{2,3}(?:_[A-Z]{2})?(?:_[a-z0-9]+)?)$")
_WP_PART = re.compile(r"(?:^|/)wp-content/(plugins|themes)/([^/]+?)(?:\.php)?(?:/|$)")
_WP_CORE = re.compile(r"(?:^|/)wp-(?:admin|includes)/")
# файлы ядра в корне WordPress; wp-config.php обновление не пишет
_WP_CORE_TOP = re.compile(r"^(?:index\.php|xmlrpc\.php|wp-(?!config\.php)[^/]+\.php)$")


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
        for k, v in (("src", str(r.get("src") or "")), ("git", _events(r.get("git"))),
                     ("wp", str(r.get("wp") or "")), ("branch", str(r.get("branch") or ""))):
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
    # helper 0.6 досмотрел файлы: пропал, вернулся к прежнему, как в HEAD, пакет перевода
    for r in b.get("roots") or []:
        if isinstance(r, dict) and r.get("key") and r.get("clean"):
            _clean(roots.get(str(r["key"])) or {}, r["clean"], now)
    if int(st["r"]) < RULES:
        _rules(roots, int(st["r"]))
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


def _rules(roots: dict, was: int) -> None:
    """Признаки, набранные по старым правилам, проходят через новые: сгенерированное уходит (с
    правил 3 - и данные Wordfence в wflogs). Набранные по helper 0.1 правки в контейнерах - тоже:
    0.1 сравнивал новый под со списком старого (ключ корня переходил к нему) и считал правкой то,
    что под пишет при старте."""
    for r in roots.values():
        if was < 2 and in_container(r):
            r.pop("edit", None)
        for k in ("php", "edit"):
            for p in [p for p in r.get(k) or {} if generated(p)]:
                r[k].pop(p)
            if k in r and not r[k]:
                r.pop(k)


CLEAN = {"gone": "файл удален", "back": "файл вернули как было", "head": "файл стал как в git (HEAD)",
         "wplang": "это пакет перевода WordPress", "plugin": "файл записал плагин по своему шаблону",
         "data": "в файле нет кода, только настройки", "deny": ".htaccess только закрывает доступ"}


def _clean(root: dict, lst, now: datetime) -> None:
    """Признаки по файлам, с которыми helper 0.6 разобрался: [[путь, почему], ...]. Почему - в
    CLEAN, у плагина с его slug ("plugin:wp-rocket", helper 0.7); php в загрузках снимается, только
    когда файла нет."""
    for x in lst or []:
        if not (isinstance(x, (list, tuple)) and len(x) >= 2 and (str(x[1]) in CLEAN or _GEN.match(str(x[1])))):
            continue
        p, (why, _sep, slug) = str(x[0]), str(x[1]).partition(":")
        for k in ("php", "edit"):
            if p not in (root.get(k) or {}):
                continue
            if k == "php" and in_uploads(p) and why != "gone":
                continue
            root[k].pop(p)
            root["cleared"] = {"at": now.isoformat(), "p": p, "why": why, **({"slug": slug} if slug else {})}
        for k in ("php", "edit"):
            if k in root and not root[k]:
                root.pop(k)


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


def _events(lst) -> list[list]:
    """[[ts, "pull"], ...] из блока helper'а 0.3, по времени."""
    out = []
    for x in lst or []:
        if isinstance(x, (list, tuple)) and x:
            try:
                out.append([int(x[0]), str(x[1] if len(x) > 1 else "")])
            except (TypeError, ValueError):
                continue
    return sorted(out)


def _git_event(root: dict, t: int, since: str = "") -> list | None:
    """Событие reflog, которым записан файл: время файла за секунды до события. Признак без
    времени файла (набран до этой версии) - событие за прогон до признака."""
    evs = root.get("git") or []
    if t > 0:
        # ближайшее событие не раньше файла: git пишет reflog после файлов
        near = [e for e in evs if e[0] - GIT_BEFORE <= t <= e[0] + GIT_AFTER]
        return min(near, key=lambda e: (e[0] < t, abs(e[0] - t)), default=None)
    try:
        s = datetime.fromisoformat(since).timestamp()
    except (TypeError, ValueError):
        return None
    return next((e for e in evs if s - RUN_WIN < e[0] <= s), None)


def _git_deploy(root: dict, ev: list, n: int) -> None:
    at = _iso(ev[0])
    old = root.get("deploy") or {}
    if old.get("at") == at and old.get("git"):
        n = max(n, int(old.get("n") or 0))
    elif old.get("at", "") > at:
        return
    root["deploy"] = {"at": at, "n": n, "git": ev[1]}


def _git_clean(root: dict) -> None:
    """Признаки, файлы которых записала выкладка через git: reflog мог прийти позже правки
    (git дописывает его в конце) или признак набран до helper'а 0.3."""
    if not root.get("git"):
        return
    for k in ("php", "edit"):
        files = root.get(k) or {}
        for p in list(files):
            if k == "php" and in_uploads(p):
                continue
            ev = _git_event(root, int(files[p].get("t") or 0), files[p].get("since") or "")
            if ev:
                files.pop(p)
                _git_deploy(root, ev, 1)
        if k in root and not files:
            root.pop(k)


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
    """[[вид, slug, было, стало, путь], ...] из прогона helper'а 0.4."""
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
    """Файлы, которые записал сам WordPress: пакет перевода, обновление плагина, темы или ядра.
    php в загрузках не объясняется ничем."""
    mo = {}
    for x in (d.get("new") or []) + (d.get("changed") or []):
        if isinstance(x, (list, tuple)) and len(x) >= 4 and isinstance(x[0], str):
            try:
                mo[x[0]] = int(x[3])
            except (TypeError, ValueError):
                pass
    out: set[str] = set()
    cand = [(p, t) for p, t, s in rows if not (is_php(p) and in_uploads(p) and not _quiet_index(p, s))]
    for p, t in cand:
        ev = _wp_lang(p, t, mo.get(p))
        if ev:
            _wp_event(root, ev)
            out.add(p)
    for up in _wp_ups(d.get("wp")):
        mine = [(p, t) for p, t in cand if p not in out and _wp_covers(up, p)]
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
    """Что helper 0.6 знает о присланных файлах: {путь: [git, владелец, веб-сервер, веб-шелл,
    вердикт 0.7]} -> {путь: {"g": ..., "o": ..., "w": 1, "x": 1, "d": "plugin:wp-rocket"}}; у 0.5 -
    только git_dirty."""
    out: dict[str, dict] = {}
    for p, v in (d.get("f") or {}).items():
        if not (isinstance(p, str) and isinstance(v, (list, tuple))):
            continue
        g, o, w, x, gen = (list(v) + ["", "", 0, 0, ""])[:5]
        i = {"g": str(g or ""), "o": str(o or "")[:32], "w": 1 if w == 1 else 0, "x": 1 if x == 1 else 0,
             "d": str(gen) if _GEN.match(str(gen or "")) and x != 1 else ""}
        out[p] = {k: v for k, v in i.items() if v}
    for p in _strs(d.get("git_dirty")):
        out.setdefault(p, {"g": "dirty"})
    for p in _strs(d.get("git_same")):
        out.setdefault(p, {"g": "same"})
    return out


def _same(root: dict, rows: list[tuple[str, int, int]], again: list[tuple[str, int, int]], ts: float) -> None:
    """Тихая строка "перезаписаны тем же содержимым": rows - файлы, ставшие как в HEAD (признак по
    такому файлу снимается: файл снова как в git), again - записанные заново тем же содержимым,
    что было (helper 0.6; признак, если был, остается - содержимое то же, что при признаке)."""
    if not rows and not again:
        return
    for p, _t, _s in rows:
        (root.get("edit") or {}).pop(p, None)
    if "edit" in root and not root["edit"]:
        root.pop("edit")
    files = sorted({p for p, _t, _s in rows + again})
    times = [t for _p, t, _s in rows + again if t > 0]
    root["same"] = {"at": _iso(max(times) if times else ts), "files": files[:5], "n": len(files), "git": bool(rows)}


def _gen(root: dict, rows: list[tuple[str, int, int]], info: dict[str, dict], ts: float) -> None:
    """Тихая строка "это файлы плагина": drop-in по шаблону, конфиг без кода, закрытый .htaccess
    (helper 0.7). Признак по такому файлу, если был, снимается: файл снова такой, каким его пишет плагин."""
    if not rows:
        return
    for p, _t, _s in rows:
        for k in ("php", "edit"):
            (root.get(k) or {}).pop(p, None)
    for k in ("php", "edit"):
        if k in root and not root[k]:
            root.pop(k)
    files = sorted({p for p, _t, _s in rows})
    times = [t for _p, t, _s in rows if t > 0]
    root["gen"] = {"at": _iso(max(times) if times else ts), "files": files[:5], "n": len(files),
                   "by": sorted({info[p]["d"] for p in files})[:5]}


def _apply(root: dict, d: dict, ts: float) -> None:
    new = [x for x in _rows(d.get("new")) if not generated(x[0])]
    changed = [x for x in _rows(d.get("changed")) if not generated(x[0])]
    info = _info(d)

    def strong(p: str, size: int) -> bool:
        # php в загрузках и php, похожий на веб-шелл, - признак при любом объяснении
        return info.get(p, {}).get("x") == 1 or is_php(p) and in_uploads(p) and not _quiet_index(p, size)

    # обновил сам WordPress - не признак и не выкладка
    wp = _wp_explain(root, d, new + changed, ts)
    new = [x for x in new if x[0] not in wp]
    changed = [x for x in changed if x[0] not in wp]
    # записанное выкладкой через git - не признак при любом числе файлов (кроме сильных)
    by_git: dict[str, list] = {}
    for p, t, _s in new + changed:
        ev = _git_event(root, t) if t > 0 else None
        if ev:
            by_git[p] = ev
    if by_git:
        last = max(by_git.values())
        _git_deploy(root, last, sum(1 for e in by_git.values() if e == last))
    # стал как в HEAD не в выкладку (IDE, инструмент, stash) - не правка, тихая строка
    head = [x for x in changed + new if info.get(x[0], {}).get("g") == "same" and x[0] not in by_git
            and not strong(x[0], x[2])]
    _same(root, head, [x for x in _rows(d.get("same")) if not generated(x[0])], ts)
    # файл, который плагин держит сам (helper 0.7), - не правка
    gen = [x for x in changed + new if info.get(x[0], {}).get("d") and x[0] not in by_git and x[0] not in wp
           and not strong(x[0], x[2])]
    _gen(root, gen, info, ts)
    quiet = wp | {p for p, _t, _s in head + gen}
    new = [x for x in new if x[0] not in quiet]
    changed = [x for x in changed if x[0] not in quiet]
    gone = d.get("gone") or []
    total = int(d.get("n_new") or len(new)) + int(d.get("n_changed") or len(changed))
    if quiet:
        # из списка (до 15 каждого вида) объяснено все - остальное, которого не видно, того же обновления
        total = total - len(quiet) if new or changed else 0
    at: dict[int, int] = {}
    for _p, t, _s in new + changed:
        if t > 0:
            at[t] = at.get(t, 0) + 1
    deploy = total >= MASS or max(at.values(), default=0) >= BATCH
    if deploy:
        root["deploy"] = {"at": _iso(ts), "n": total}
    since = _iso(ts)
    for p, t, size in new:
        if strong(p, size):
            _add(root, "php", p, since, size, t, {"k": "new", **info.get(p, {})})
        elif deploy or p in by_git:
            continue
        else:
            _add(root, "php" if is_php(p) else "edit", p, since, size, t, {"k": "new", **info.get(p, {})})
    for p, t, size in changed:
        if strong(p, size):
            _add(root, "php", p, since, size, t, {"k": "chg", **info.get(p, {})})
        elif not deploy and p not in by_git:
            _add(root, "edit", p, since, size, t, {"k": "chg", **info.get(p, {})})
    for g in gone:
        p = g[0] if isinstance(g, (list, tuple)) and g else g
        for k in ("php", "edit"):
            (root.get(k) or {}).pop(str(p), None)
    for k in ("php", "edit"):
        if k in root and not root[k]:
            root.pop(k)


_FILE_KEYS = ("k", "g", "o", "w", "x")


def _add(root: dict, key: str, p: str, since: str, size: int, t: int = 0, about: dict | None = None) -> None:
    """about - что известно о файле: k (new/chg), g (как в git: dirty/new/ignored), o - владелец,
    w - записал пользователь веб-сервера, x - похож на веб-шелл. Время признака - первое."""
    files = root.setdefault(key, {})
    if p in files or len(files) < MAX_FILES:
        old = files.get(p) or {}
        files[p] = {"since": old.get("since") or since, "s": size}
        if t > 0:
            files[p]["t"] = t
        about = about or {}
        # первый раз новый - так и остается, веб-шелл однажды - тоже
        if old.get("k") == "new":
            about = {**about, "k": "new"}
        if old.get("x"):
            about = {**about, "x": 1}
        for k in _FILE_KEYS:
            if about.get(k):
                files[p][k] = about[k]


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


# ---------- где правят руками ----------

def _mkey(r: dict) -> str:
    """Ключ режима: ворклоад и путь, а не под - новый под того же деплоймента режим не теряет."""
    return f"{workload(r)}:{r.get('path') or ''}"


def set_mode(state: dict | None, root: str, mode: str) -> dict:
    """Режим корней строки (root - ключи через запятую): auto - по ветке, dev - здесь правят
    руками, strict - следить за каждой правкой."""
    st = json.loads(json.dumps(state or {}))
    modes = st.setdefault("modes", {})
    for rk in root.split(","):
        r = (st.get("roots") or {}).get(rk)
        if r is None:
            continue
        if mode == "auto":
            modes.pop(_mkey(r), None)
        else:
            modes[_mkey(r)] = mode
    if not modes:
        st.pop("modes")
    return st


def mode(state: dict | None, r: dict) -> tuple[bool, str]:
    """(правят ли здесь руками, как выбрано: auto/dev/strict). Сам - рабочая копия git на ветке,
    с которой не выкладывают (develop, feature/...): там незакоммиченные правки - работа."""
    m = ((state or {}).get("modes") or {}).get(_mkey(r), "auto")
    if m in ("dev", "strict"):
        return m == "dev", m
    b = r.get("branch") or ""
    return bool(b) and not _PROD_BRANCH.match(b), "auto"


def dev_ok(k: str, p: str, v: dict) -> bool:
    """Признак, который там, где правят руками, - работа разработчика, а не взлом. Остаются php в
    загрузках, похожее на веб-шелл и записанное самим сайтом (пользователь веб-сервера или php)."""
    return not (v.get("x") or v.get("w") or k == "php" and in_uploads(p))


def signs(state: dict | None, r: dict, k: str) -> dict[str, dict]:
    """Признаки корня, которые показываем: где правят руками - без работы разработчика."""
    files = r.get(k) or {}
    if not mode(state, r)[0]:
        return files
    return {p: v for p, v in files.items() if not dev_ok(k, p, v)}


def _dev_work(state: dict | None, items: list[tuple[str, dict]]) -> dict[str, dict]:
    """Правки разработчика, которые не признак: для серой строки."""
    out: dict[str, dict] = {}
    for _rk, r in items:
        if not mode(state, r)[0]:
            continue
        for k in ("php", "edit"):
            for p, v in (r.get(k) or {}).items():
                if dev_ok(k, p, v) and (p not in out or v.get("since", "") < out[p].get("since", "")):
                    out[p] = v
    return out


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
    """(ворклоады, путь, корни): поды одного ворклоада с одним путем - одна строка, один каталог,
    смонтированный в несколько контейнеров (src от helper'а 0.3), - тоже, с контейнерами через
    запятую."""
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


def _merged(items: list[tuple[str, dict]], k: str, state: dict | None = None) -> dict[str, dict]:
    """Признаки строки (поды одного ворклоада): каждый файл раз, с самым ранним временем."""
    out: dict[str, dict] = {}
    for _rk, r in items:
        for p, v in signs(state, r, k).items():
            if p not in out or v.get("since", "") < out[p].get("since", ""):
                out[p] = v
    return out


def mirror(servers: list[tuple[int, str, dict]], checks: list[tuple[int, str, set[int]]]):
    """({check_id: {"php": sign, "edit": sign}}, {server_id: [ключи непривязанных корней]})."""
    by_check: dict[int, dict] = {}
    loose: dict[int, list[str]] = {}
    for sid, name, st in servers:
        for rk, r in ((st or {}).get("roots") or {}).items():
            if not (signs(st, r, "php") or signs(st, r, "edit")):
                continue
            own = owners(r, sid, checks)
            if not own:
                loose.setdefault(sid, []).append(rk)
                continue
            for cid in own:
                for k in ("php", "edit"):
                    for p, v in signs(st, r, k).items():
                        sign = by_check.setdefault(cid, {}).setdefault(k, {"files": [], "since": v["since"]})
                        f = {"p": p, "sid": sid, "root": rk, "where": where(name, r), "since": v["since"],
                             **{x: v[x] for x in ("t",) + _FILE_KEYS if v.get(x)}}
                        if r.get("wp"):
                            f["wp"] = True
                        if v.get("g"):
                            f["branch"] = r.get("branch") or ""
                        sign["files"].append(f)
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
        pf, ef = _merged(items, "php", state), _merged(items, "edit", state)
        php, edit = sorted(pf), sorted(ef)
        if php:
            text = f"похоже на взлом: {php_head(pf)} в {path}{at}: " + tg_fmt.names([_base(p) for p in php])
            text += _owner_txt(pf)
            if edit:
                text += ", правки не через git/деплой: " + tg_fmt.names([_base(p) for p in edit], 2)
            out.append((2, text, f"php:{gk}:" + ",".join(php)))
        elif edit:
            head = edit_head(edit, any(r.get("wp") for _rk, r in items), _git_kind(ef),
                             next((r["branch"] for _rk, r in items if r.get("branch")), ""))
            out.append((1, f"{head} в {path}{at}: " + tg_fmt.names([_base(p) for p in edit]) + _owner_txt(ef),
                        f"edit:{gk}:" + ",".join(edit)))
    return out


def php_head(files: dict[str, dict]) -> str:
    """Что за php: похожий на веб-шелл - так и сказать, иначе новые."""
    return "php похож на веб-шелл" if any(v.get("x") for v in files.values()) else "новые php"


def _owner_txt(files: dict[str, dict]) -> str:
    """У всех файлов один владелец - кто, и записал ли их сам сайт."""
    own = {v.get("o", "") for v in files.values()}
    if len(own) != 1 or "" in own:
        return ""
    web = all(v.get("w") for v in files.values())
    return f", владелец {own.pop()}" + (" (веб-сервер)" if web else "")


def _git_kind(files: dict[str, dict]) -> str:
    """Что о файлах правки знает git, если у всех одно: dirty, new или ignored."""
    kinds = {v.get("g", "") for v in files.values()}
    return kinds.pop() if len(kinds) == 1 else ""


GIT_HEADS = {"dirty": "не закоммиченная правка в рабочей копии git",
             "new": "новые файлы, которых нет в git",
             "ignored": "изменены файлы вне git (в .gitignore)"}


def edit_head(paths: list[str], versions: bool, git: str | bool = "", branch: str = "") -> str:
    """Как назвать правку: что о всех файлах знает git (dirty - не как в HEAD, new - git их не
    знает, ignored - в .gitignore), иначе все файлы одного плагина, темы или ядра WordPress - так и
    сказать. versions - helper следит за версиями WordPress, т.е. версия точно не сменилась."""
    if git is True:
        git = "dirty"
    if git in GIT_HEADS:
        return GIT_HEADS[git] + (f" (ветка {branch})" if branch else "")
    parts = {wp_part(p, versions) for p in paths}
    if len(parts) == 1 and None not in parts:
        kind, slug = parts.pop()
        what = {"plugin": f"файлы плагина {slug}", "theme": f"файлы темы {slug}",
                "core": "файлы ядра WordPress"}[kind]
        return what + (" изменились без смены версии" if versions else " изменились")
    return "код изменен не через git/деплой"


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
        dev, how = mode(state, rs[0])
        work = _dev_work(state, items)
        out.append({
            # key - корни через запятую: "это наше" снимает признаки со всех подов строки
            "key": ",".join(rk for rk, _r in items), "where": w or "host", "path": path,
            # подов самого большого ворклоада строки: php-fpm и воркер одного проекта - не поды
            "pods": max(Counter(workload(r) for r in rs).values()) if w else 0,
            "domains": list(dict.fromkeys(d for r in rs for d in r.get("domains") or []))[:20],
            "files": max(r.get("files", 0) for r in rs), "big": any(r.get("big") for r in rs),
            "layer": all(r.get("layer") for r in rs),
            "deploy": max(deploys, key=lambda x: x.get("at", "")) if deploys else None,
            "php": [{"p": p, **v} for p, v in sorted(_merged(items, "php", state).items())],
            "edit": [{"p": p, **v} for p, v in sorted(_merged(items, "edit", state).items())],
            # здесь правят руками (dev) и как это выбрано (auto - по ветке); правки разработчика -
            # серой строкой, не признак
            "dev": dev, "mode": how,
            "dev_work": [{"p": p, **v} for p, v in sorted(work.items(), key=lambda x: x[1].get("since", ""),
                                                           reverse=True)[:MAX_FILES]],
            # последний признак, который снялся сам: файл удалили, вернули, закоммитили (за неделю)
            "cleared": _cleared_shown(rs, now),
            "site": any(rk not in loose for rk, _r in items),
            # WordPress: версия ядра (helper 0.4 следит за версиями) и что он обновил сам за неделю
            "wp": next((r["wp"] for r in rs if r.get("wp")), ""),
            "wp_events": _wp_shown(rs, now),
            # рабочая копия git: ветка и последний файл, записанный заново тем же содержимым (за неделю)
            "branch": next((r["branch"] for r in rs if r.get("branch")), ""),
            "same": _same_shown(rs, now),
            # файлы, которые плагин записал сам: drop-in по шаблону, конфиг без кода (за неделю)
            "gen": _gen_shown(rs, now),
        })
    return {"roots": out, "ts": (b or {}).get("ts")}


def _cleared_shown(rs: list[dict], now: datetime) -> dict | None:
    last = max((r["cleared"] for r in rs if r.get("cleared")), key=lambda x: x.get("at", ""), default=None)
    try:
        return last if last and now - datetime.fromisoformat(last["at"]) <= WP_SHOW else None
    except (KeyError, ValueError):
        return None


def _same_shown(rs: list[dict], now: datetime, k: str = "same") -> dict | None:
    last = max((r[k] for r in rs if r.get(k)), key=lambda x: x.get("at", ""), default=None)
    try:
        return last if last and now - datetime.fromisoformat(last["at"]) <= WP_SHOW else None
    except (KeyError, ValueError):
        return None


def _gen_shown(rs: list[dict], now: datetime) -> dict | None:
    return _same_shown(rs, now, "gen")


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
    what, _sep, place = head.partition(" в ")
    return f"Похоже на взлом: {what}", [files.split(", правки")[0], place]


# ---------- стадия планировщика ----------

async def track(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Свежие блоки helper'а -> servers.webfiles; признаки привязанных корней -> мониторы."""
    from app import parents as parents_mod
    from app import tamper as tamper_mod
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
                # "отложить" в карточке сайта прячет файлы, что были в признаке, новые видно сразу
                files = [f for f in (want.get(k) or {}).get("files") or []
                         if not tamper_mod.hidden(st.get("post"), k, f["p"], now)]
                if files:
                    signs[k] = {"files": files, "since": min(f["since"] for f in files)}
                else:
                    signs.pop(k, None)
            if before != {k: signs.get(k) for k in ("php", "edit")}:
                c.tamper_state = {**st, "signs": signs}
                dirty = True
        if dirty:
            await s.commit()

