"""GitLab в "Сервисах": версия, путь обновления, бэкап, фоновые миграции, автообновление; алерт "GitLab устарел".

Свой GitLab отстает тихо: работает на версии с известными дырами, а путь наверх с каждой
пропущенной обязательной остановкой (upgrade path) длиннее. Свой бэкап тоже легко сломать незаметно:
крон с SKIP=repositories делает архив без кода, а без gitlab-secrets.json базу из архива не прочитать.

Helper gitlab-setup раз в 15 минут находит контейнеры GitLab на ноде (том /var/opt/gitlab или образ
gitlab/gitlab-ce|ee) и пишет extras "gitlab": {"v": 1, "ts", "instances": [{"ct", "state", "image",
"dir", "service", "files", "mode" (image|build), "from", "dockerfile", "version", "edition", "url",
"data", "config", "secrets", "bdir", "keep", "bk": {"n", "total", "first", "last": {"name", "ts",
"size", "info", "skipped", "ver"}}, "cfg": {"n", "ts", "path"}, "bbm": {"total", "failed", "open",
"list": [{"st", "job", "table", "col"}]}, "au": {"conf", "timer", "next", "svc", "result", "blocked",
"broken", "behind_since", "last_result", "history", "gitlab_backup", "skip", "allow_major",
"stale_days"}, "sched": [{"how", "src", "when", "db", "skip", "etc", "enabled"?, "last"?, "next"?,
"result"?}]}]}.

Какие версии вышли, панель узнает сама, одной выборкой на все серверы (refresh): теги gitlab-ce и
gitlab-ee с Docker Hub с датой выкладки и обязательные остановки из upgrade_path.yml GitLab. Хранится в
app_settings "gitlab_feed", раз в REFRESH_EVERY, при ошибке остается прежняя. Оценка алертов берет ее
оттуда же (load_feed), а без выпусков объявленный алерт замирает: иначе каждый перезапуск планировщика
давал отбой и через минуту ту же тревогу. Запросы только по
IPv4: на одной ноде панели IPv6 до gitlab.com битый, и клиент висел на каждом AAAA.

Алерт "gitlab" - не на каждый патч, а когда пора вмешаться:
- своя минорная получила патч (у GitLab это выпуски с исправлениями безопасности), а он не стоит
  дольше PATCH_DAYS дней;
- версия ниже последней вышедшей обязательной остановки, и вышла она больше STOP_DAYS дней назад:
  дальше придется идти по шагам, и путь только растет;
- gitlab-autoupgrade заблокировал версию после отката или пометил GitLab сломанным;
- упали фоновые миграции (batched_background_migrations, status 4): следующее обновление на них
  остановится.
Незавершенные миграции, бэкап и выключенный таймер автообновления только показываются.

Заглушить инстанс: alert_mutes "gitlab:<ключ>" насовсем или alert_snoozes на время; ключ - хост из
external_url, без него имя контейнера.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app import tg_fmt
from app.models import Server

log = logging.getLogger("kervax.gitlab")

MUTE_PREFIX = "gitlab:"
MAX_AGE = 3600  # блок старше часа - helper встал, судить не по чему
PATCH_DAYS = 7  # патч своей минорной не стоит дольше - алерт
STOP_DAYS = 14  # версия ниже обязательной остановки, вышедшей больше стольких дней назад - алерт
BACKUP_OLD_H = 48  # последний gitlab-backup старше - подсветка в разделе
FEED_KEY = "gitlab_feed"
REFRESH_EVERY = timedelta(hours=6)
RETRY_AFTER = timedelta(hours=1)
_LOOK_EVERY = timedelta(minutes=10)
HUB_URL = "https://hub.docker.com/v2/repositories/gitlab/gitlab-{ed}/tags?page_size=100&name={major}."
STOPS_URL = "https://gitlab.com/gitlab-org/gitlab/-/raw/master/config/upgrade_path.yml"
# компоненты бэкапа, без которых GitLab из него не поднять целиком. registry и artifacts пропускают
# все (их много, и они не нужны для восстановления), builds - логи джоб
_SKIP_KEY = {"db": "базы", "repositories": "репозиториев", "uploads": "загрузок", "lfs": "LFS",
             "packages": "пакетов", "pages": "Pages", "terraform_state": "состояний Terraform",
             "ci_secure_files": "секретных файлов CI"}
_BBM = {0: "на паузе", 1: "идет", 4: "упала", 5: "финализируется"}

_feed: dict | None = None
_feed_at = 0.0
_running: asyncio.Task | None = None
_last_look: datetime | None = None


# ---- блок helper'а ----

def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("gitlab")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def instances(rep: dict) -> list[dict]:
    return [x for x in (block(rep) or {}).get("instances") or [] if isinstance(x, dict)]


def host(x: dict) -> str:
    try:
        return urlparse(str(x.get("url") or "")).hostname or ""
    except ValueError:
        return ""


def key(x: dict) -> str:
    return host(x) or str(x.get("ct") or "")


def label(x: dict) -> str:
    return host(x) or f"контейнер {x.get('ct') or '?'}"


def _i(d: dict, k: str) -> int:
    try:
        return int((d or {}).get(k) or 0)
    except (TypeError, ValueError):
        return 0


def _vt(s: str) -> tuple[int, int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", str(s or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _vs(t) -> str:
    return ".".join(str(x) for x in t)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


# ---- выпуски GitLab: одна выборка на панель ----

def feed() -> dict | None:
    return _feed


def set_feed(d: dict | None) -> None:
    global _feed, _feed_at
    _feed, _feed_at = d, time.monotonic()


async def load_feed(session, ttl: float = 300) -> dict | None:
    """Выпуски из app_settings в память процесса (веб читает то, что собрал планировщик)."""
    if _feed is not None and time.monotonic() - _feed_at < ttl:
        return _feed
    from app import settings_store
    raw = await settings_store.get_raw(session, FEED_KEY)
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        d = None
    set_feed(d if isinstance(d, dict) else None)
    return _feed


def parse_tags(results: list, ed: str) -> dict[str, int]:
    """Теги Docker Hub -> {"19.4.1": время выкладки}; только выпуски X.Y.Z-ce.0 / -ee.0 (без rc)"""
    out: dict[str, int] = {}
    for t in results or []:
        if not isinstance(t, dict):
            continue
        m = re.match(r"^(\d+\.\d+\.\d+)-%s\.0$" % ed, str(t.get("name") or ""))
        if not m:
            continue
        when = str(t.get("tag_last_pushed") or t.get("last_updated") or "")
        try:
            ts = int(datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
        except ValueError:
            ts = 0
        out[m.group(1)] = ts
    return out


def parse_stops(txt: str) -> list[tuple[int, int]]:
    return sorted({(int(a), int(b)) for a, b in re.findall(r"major:\s*(\d+)\s*\n\s*minor:\s*(\d+)", txt or "")})


def _needed(servers) -> set[tuple[str, int]]:
    """(редакция, мажорная) по парку и следующая мажорная - чтобы видеть ее выход"""
    out: set[tuple[str, int]] = set()
    for s in servers:
        for x in instances(s.last_report or {}):
            v, ed = _vt(x.get("version") or ""), str(x.get("edition") or "")
            if v and ed in ("ce", "ee"):
                out |= {(ed, v[0]), (ed, v[0] + 1)}
    return out


async def fetch(need: set[tuple[str, int]], old: dict | None) -> dict:
    """Теги и остановки. IPv4: local_address 0.0.0.0 - клиент не пробует AAAA"""
    tags: dict[str, dict[str, int]] = {}
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
    async with httpx.AsyncClient(transport=transport, timeout=60, follow_redirects=True,
                                 headers={"User-Agent": "Kervax GitLab versions"}) as client:
        for ed, major in sorted(need):
            url: str | None = HUB_URL.format(ed=ed, major=major)
            pages = 0
            while url and pages < 10:
                r = await client.get(url)
                r.raise_for_status()
                d = r.json()
                tags.setdefault(ed, {}).update(
                    {v: ts for v, ts in parse_tags(d.get("results") or [], ed).items() if v.startswith(f"{major}.")})
                url, pages = d.get("next"), pages + 1
        stops, src = [], "upgrade_path.yml"
        try:
            r = await client.get(STOPS_URL)
            r.raise_for_status()
            stops = parse_stops(r.text)
            if len(stops) < 10:
                raise ValueError("мало остановок")
        except Exception as e:  # noqa: BLE001 - остановки из прошлой выборки или по правилу
            log.warning("upgrade_path.yml не скачался: %s", e)
            stops = [tuple(x) for x in (old or {}).get("stops") or []]
            src = "прошлая выборка upgrade_path.yml" if stops else "правило X.2/X.5/X.8/X.11"
    if not stops:
        top = max((m for _, m in need), default=19)
        stops = [(M, m) for M in range(17, top + 2) for m in (2, 5, 8, 11)]
    return {"tags": tags, "stops": [list(s) for s in stops], "stops_src": src}


async def refresh(session_factory, now: datetime) -> None:
    """Раз в 10 минут посмотреть, пора ли обновить выпуски (раз в REFRESH_EVERY или когда в парке
    появилась новая редакция или мажорная), и обновить фоном: тик планировщика загрузку не ждет."""
    global _running, _last_look
    if _running is not None and not _running.done():
        return
    if _last_look is not None and now - _last_look < _LOOK_EVERY:
        return
    _last_look = now
    from sqlalchemy import select

    from app import settings_store
    async with session_factory() as session:
        servers = (await session.scalars(select(Server).where(Server.enabled.is_(True)))).all()
        need = _needed(servers)
        old = await load_feed(session, ttl=0)
    if not need:
        return
    have = {(ed, int(v.split(".")[0])) for ed, vs in ((old or {}).get("tags") or {}).items() for v in vs}
    ts = _dt((old or {}).get("ts"))
    tried = _dt((old or {}).get("tried"))
    fresh = ts is not None and now - ts < REFRESH_EVERY and not (need - have - _empty(old))
    wait = bool((old or {}).get("err")) and tried is not None and now - tried < RETRY_AFTER
    if fresh or wait:
        return

    async def run() -> None:
        d = dict(old or {})
        try:
            got = await fetch(need, old)
            # мажорная без единого тега (20 еще не вышла) - не повод тянуть ее каждые 10 минут
            got["empty"] = sorted([ed, m] for ed, m in need if not any(
                v.startswith(f"{m}.") for v in (got["tags"].get(ed) or {})))
            d = {**got, "ts": now.isoformat(), "err": ""}
        except Exception as e:  # noqa: BLE001 - прежние данные остаются
            d["err"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("выпуски GitLab не обновились: %s", d["err"])
        d["tried"] = now.isoformat()
        async with session_factory() as session:
            await settings_store.set_raw(session, FEED_KEY, json.dumps(d))
            await session.commit()
        set_feed(d)

    _running = asyncio.create_task(run())


def _empty(fd: dict | None) -> set[tuple[str, int]]:
    return {(str(a), int(b)) for a, b in (fd or {}).get("empty") or []}


def _dt(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---- версия против выпусков ----

def status(x: dict, fd: dict | None, now: datetime) -> dict | None:
    """Что с версией инстанса: {"latest", "lp" (последний патч своей минорной), "patch" ({"ver",
    "days"} - самый старый не поставленный патч своей минорной), "stop" ({"ver", "days"} - ниже
    последней вышедшей остановки), "next_stop", "target", "path", "major" (вышла новая мажорная)}.
    None - версия неизвестна (контейнер лежит) или выпусков еще нет."""
    cur = _vt(x.get("version") or "")
    ed = str(x.get("edition") or "")
    tags = {_vt(v): int(ts or 0) for v, ts in (((fd or {}).get("tags") or {}).get(ed) or {}).items()}
    tags.pop(None, None)
    if not cur or not tags:
        return None
    stops = sorted({(int(a), int(b)) for a, b in (fd or {}).get("stops") or []})
    nowt = now.timestamp()

    def lp(k) -> tuple | None:
        c = [v for v in tags if v[:2] == k]
        return max(c) if c else None

    def minor_out(k) -> int:  # когда вышла минорная: тег .0, иначе самый ранний ее тег
        c = [ts for v, ts in tags.items() if v[:2] == k]
        z = tags.get((k[0], k[1], 0))
        return z if z else (min(c) if c else 0)

    newest = max(tags)
    same = [v for v in tags if v[0] == cur[0]]
    target = max(same) if same else None
    out: dict = {"latest": _vs(newest), "lp": None, "patch": None, "stop": None, "next_stop": None,
                 "target": None, "path": [], "major": None}
    own = lp(cur[:2])
    if own:
        out["lp"] = _vs(own)
    pend = sorted(v for v in tags if v[:2] == cur[:2] and v > cur)
    if pend:
        first = pend[0]
        out["patch"] = {"ver": _vs(first), "newest": _vs(pend[-1]),
                        "days": max(0, int((nowt - tags[first]) // 86400)) if tags[first] else None}
    # последняя вышедшая остановка и, если версия ниже нее, сколько она уже есть
    released = [s for s in stops if lp(s)]
    if released and cur[:2] < released[-1]:
        s = released[-1]
        t0 = minor_out(s)
        out["stop"] = {"ver": f"{s[0]}.{s[1]}", "days": max(0, int((nowt - t0) // 86400)) if t0 else None}
    nxt = [s for s in stops if s > cur[:2]]
    if nxt:
        out["next_stop"] = f"{nxt[0][0]}.{nxt[0][1]}"
    if newest[0] > cur[0]:
        out["major"] = _vs(newest)
    if target and target > cur:
        # как в gitlab-autoupgrade (cmd_path): на остановке сперва ее последний патч, потом каждая
        # остановка между текущей и целевой минорной последним патчем, потом цель
        path, pos = [], cur
        if cur[:2] in stops and own and own > cur and target[:2] != cur[:2]:
            path.append(own)
            pos = own
        for s in stops:
            if pos[:2] < s < target[:2]:
                p = lp(s)
                if p:
                    path.append(p)
                    pos = p
        if target > pos:
            path.append(target)
        out["target"], out["path"] = _vs(target), [_vs(v) for v in path]
    return out


def _au_blocked(x: dict) -> tuple[str, str] | None:
    """Блокировка gitlab-autoupgrade: "<версия> <дата> <время> <причина>" -> (версия, причина)"""
    b = str(((x.get("au") or {}).get("blocked")) or "").strip()
    if not b:
        return None
    p = b.split(" ", 3)
    return (p[0], p[3] if len(p) > 3 else "")


def _au_broken(x: dict) -> str:
    """Признак "сломан": "<дата> <время> <с> -> <на>: <причина>" -> "<с> -> <на>: <причина>" """
    b = str(((x.get("au") or {}).get("broken")) or "").strip()
    return b.split(" ", 2)[2] if b.count(" ") >= 2 else b


def causes(x: dict, fd: dict | None, now: datetime) -> list[str]:
    """Поводы алерта: patch, stop, blocked, broken, bbm"""
    out = []
    st = status(x, fd, now)
    if st:
        p = st.get("patch")
        if p and p.get("days") is not None and p["days"] >= PATCH_DAYS:
            out.append("patch")
        s = st.get("stop")
        if s and s.get("days") is not None and s["days"] >= STOP_DAYS:
            out.append("stop")
    if _au_broken(x):
        out.append("broken")
    elif _au_blocked(x):
        out.append("blocked")
    if _i(x.get("bbm") or {}, "failed") > 0:
        out.append("bbm")
    return out


def _failed_jobs(x: dict) -> list[str]:
    return [str(r.get("job") or "?") for r in ((x.get("bbm") or {}).get("list") or [])
            if isinstance(r, dict) and _i(r, "st") == 4]


def why(x: dict, fd: dict | None, now: datetime, cs: list[str] | None = None) -> str:
    cs = causes(x, fd, now) if cs is None else cs
    st = status(x, fd, now) or {}
    cur, parts = str(x.get("version") or ""), []
    if "patch" in cs:
        p = st["patch"]
        parts.append(f"{cur}, патч {p['ver']} вышел {tg_fmt.days(p['days'])} назад"
                     + (f" (последний {p['newest']})" if p["newest"] != p["ver"] else ""))
    if "stop" in cs:
        s = st["stop"]
        parts.append(f"{cur} ниже обязательной остановки {s['ver']} (вышла {tg_fmt.days(s['days'])} назад),"
                     f" обновлять по шагам: {' -> '.join(st.get('path') or [])}")
    if "broken" in cs:
        parts.append(f"автообновление пометило GitLab сломанным, откат не удался: {_au_broken(x)}")
    if "blocked" in cs:
        v, r = _au_blocked(x) or ("", "")
        parts.append(f"автообновление остановлено на {v}" + (f": {r}" if r else ""))
    if "bbm" in cs:
        n = _i(x["bbm"], "failed")
        jobs = _failed_jobs(x)
        parts.append(f"упали фоновые миграции ({n})" + (f": {', '.join(jobs[:2])}" if jobs else "")
                     + ", следующее обновление на них остановится")
    return "; ".join(parts)


def _muted(s: Server, now: datetime) -> set[str]:
    keys = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        if not str(k).startswith(MUTE_PREFIX):
            continue
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) > now:
            keys.add(k[len(MUTE_PREFIX):])
    return keys


def muted_keys(s: Server, now: datetime) -> set[str]:
    return _muted(s, now)


def problems(s: Server, now: datetime) -> list[dict]:
    """Инстансы, где пора вмешаться, без заглушенных: {"key", "label", "level", "why", "text", "causes"}"""
    fd, muted = feed(), _muted(s, now)
    out = []
    for x in instances(s.last_report or {}):
        k = key(x)
        cs = causes(x, fd, now)
        if not cs or k in muted:
            continue
        w = why(x, fd, now, cs)
        st = status(x, fd, now) or {}
        out.append({"key": k, "label": label(x), "level": 2 if "broken" in cs else 1, "why": w,
                    "text": f"GitLab {label(x)}: {w}", "causes": cs, "version": str(x.get("version") or ""),
                    "lp": (st.get("patch") or {}).get("newest") or "", "patch_days": (st.get("patch") or {}).get("days"),
                    "stop": (st.get("stop") or {}).get("ver") or "", "target": st.get("target") or "",
                    "blocked": (_au_blocked(x) or ("", ""))[0], "bbm_jobs": _failed_jobs(x),
                    "bbm": _i(x.get("bbm") or {}, "failed")})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----

def tg_short(items: list[dict]) -> tuple[str, list[str]] | None:
    """"GitLab устарел / gitlab.example.com: 19.4.1, есть 19.4.3 (патч 11 дней)". Что - про худший
    инстанс (первый), остальные именами"""
    if not items:
        return None
    x = items[0]
    cs = x.get("causes") or []
    name = x["label"]
    if "broken" in cs:
        what, facts = "GitLab сломан после обновления", [tg_fmt.fact("", f"{name}: откат не удался")]
    elif "bbm" in cs:
        n = int(x.get("bbm") or 0)
        jobs = x.get("bbm_jobs") or []
        what = "Упали фоновые миграции GitLab"
        facts = [tg_fmt.fact("", f"{name}: {jobs[0] if jobs else n}" + (f" и еще {n - 1}" if n > 1 else ""))]
    elif "blocked" in cs and "patch" not in cs and "stop" not in cs:
        what, facts = "Автообновление GitLab остановлено", [tg_fmt.fact("", f"{name}: откат с {x.get('blocked') or '?'}")]
    else:
        what = "GitLab устарел"
        if "stop" in cs:
            fact = f"{name}: {x['version']}, ниже остановки {x['stop']}"
        else:
            d = x.get("patch_days")
            fact = f"{name}: {x['version']}, есть {x['lp']}" + (f" (патч {tg_fmt.days(d)})" if d is not None else "")
        facts = [tg_fmt.fact("", fact)]
        if "blocked" in cs:
            facts.append("автообновление остановлено после отката")
    rest = [y["label"] for y in items[1:]]
    if rest:
        facts.append(tg_fmt.fact("Еще", tg_fmt.names(rest)))
    return what, facts[:2]


# ---- раздел "GitLab" ----

def _under(p: str, base: str) -> bool:
    base = base.rstrip("/") or "/"
    return base == "/" or p == base or p.startswith(base + "/")


def restic_covers(rep: dict, path: str) -> bool:
    """Покрыт ли путь файловым бэкапом Kervax (restic): include - под одним из путей, exclude - не под
    исключением. Без настроенного бэкапа - нет"""
    bk = (rep or {}).get("backup") or {}
    if not path or not bk.get("configured"):
        return False
    inc = [p for p in bk.get("includes") or [] if str(p).startswith("/")]
    exc = [p for p in bk.get("excludes") or [] if str(p).startswith("/")]
    if (bk.get("mode") or "exclude") == "include":
        return any(_under(path, p) for p in inc)
    return not any(_under(path, p) for p in exc)


def backup_notes(x: dict, rep: dict, now: datetime) -> list[dict]:
    """Что не так с бэкапом инстанса, словами: [{"level", "text"}]. Только показ"""
    out = []
    bk = x.get("bk") or {}
    last = bk.get("last") or {}
    nowt = now.timestamp()
    sched = [r for r in x.get("sched") or [] if isinstance(r, dict) and _i(r, "db")]
    if not x.get("bdir"):
        out.append({"level": 1, "text": "каталог бэкапов не найден на хосте"})
    elif not _i(bk, "n"):
        out.append({"level": 2, "text": "в каталоге бэкапов нет ни одного архива gitlab-backup"})
    else:
        age = nowt - _i(last, "ts")
        if age > BACKUP_OLD_H * 3600:
            out.append({"level": 2 if sched else 1,
                        "text": f"последний бэкап {tg_fmt.days(int(age // 86400))} назад"
                                + ("" if sched else ", по расписанию не делается")})
        sk = [p for p in str(last.get("skipped") or "").split(",") if p]
        lost = [_SKIP_KEY[p] for p in sk if p in _SKIP_KEY]
        if lost:
            out.append({"level": 2, "text": f"бэкап без {', '.join(lost)} (SKIP={','.join(sk)})"})
    if not sched and (not _i(bk, "n") or nowt - _i(last, "ts") <= BACKUP_OLD_H * 3600):
        out.append({"level": 1, "text": "крон или таймер с gitlab-backup не найден"})
    for r in sched:
        sk = [p for p in str(r.get("skip") or "").split(",") if p]
        lost = [_SKIP_KEY[p] for p in sk if p in _SKIP_KEY]
        if lost and not any(lost[0] in n["text"] for n in out):
            out.append({"level": 2, "text": f"расписание делает бэкап без {', '.join(lost)} (SKIP={','.join(sk)})"})
        if r.get("result") and r.get("result") != "success":
            out.append({"level": 2, "text": f"последний запуск {r.get('src')} завершился с ошибкой ({r.get('result')})"})
    cfg = x.get("cfg") or {}
    in_restic = restic_covers(rep, str(x.get("config") or ""))
    if not cfg and not in_restic:
        out.append({"level": 2, "text": "gitlab-secrets.json и конфиг нигде не сохраняются: без них базу из бэкапа не прочитать"})
    elif cfg and not in_restic and nowt - _i(cfg, "ts") > 30 * 86400:
        out.append({"level": 1, "text": f"архиву конфига {tg_fmt.days(int((nowt - _i(cfg, 'ts')) // 86400))}, в restic-бэкапе Kervax его нет"})
    return out


_INST = ("ct", "state", "image", "dir", "service", "files", "mode", "from", "dockerfile", "version", "edition",
         "url", "data", "config", "bdir")


def view(s: Server, now: datetime) -> dict | None:
    """Для раздела "GitLab": {"ts", "feed": {"ts", "err", "stops_src"}, "patch_days", "stop_days",
    "instances": [...]}; у инстанса поля helper'а, "key", "label", "st" (status), "bk", "sched",
    "cfg", "secrets", "restic_cfg", "restic_data", "notes" (бэкап), "bbm", "au", беда ("level",
    "why", "causes") и мьют"""
    rep = s.last_report or {}
    b = block(rep)
    if b is None:
        return None
    fd = feed()
    muted = _muted(s, now)
    perm = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    snz = {str(k)[len(MUTE_PREFIX):]: str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()
           if str(k).startswith(MUTE_PREFIX)}
    rows = []
    for x in instances(rep):
        k = key(x)
        row = {f: str(x.get(f) or "") for f in _INST}
        au = x.get("au") if isinstance(x.get("au"), dict) else None
        row.update({
            "key": k, "label": label(x), "host": host(x), "secrets": bool(_i(x, "secrets")), "keep": _i(x, "keep"),
            "st": status(x, fd, now), "bk": x.get("bk") if isinstance(x.get("bk"), dict) else None,
            "sched": [r for r in x.get("sched") or [] if isinstance(r, dict)],
            "cfg": x.get("cfg") if isinstance(x.get("cfg"), dict) else None,
            "restic_cfg": restic_covers(rep, str(x.get("config") or "")),
            "restic_data": restic_covers(rep, str(x.get("data") or "")),
            "notes": backup_notes(x, rep, now),
            "bbm": x.get("bbm") if isinstance(x.get("bbm"), dict) else None,
            "au": au,
        })
        cs = causes(x, fd, now)
        if cs:
            row["level"], row["why"], row["causes"] = (2 if "broken" in cs else 1), why(x, fd, now, cs), cs
        if k in perm:
            row["mute"] = {"perm": True}
        elif k in muted and k in snz:
            row["mute"] = {"until": snz[k]}
        rows.append(row)
    f = fd or {}
    return {"ts": int(b.get("ts") or 0), "patch_days": PATCH_DAYS, "stop_days": STOP_DAYS,
            "feed": {"ts": str(f.get("ts") or ""), "err": str(f.get("err") or ""),
                     "stops_src": str(f.get("stops_src") or "")} if fd else None,
            "instances": rows}
