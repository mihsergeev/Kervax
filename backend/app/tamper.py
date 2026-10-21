"""Признаки взлома сайта: монитор видит 200, а с сайтом что-то не так.

Раз в час (после первого подозрения - через 10 минут, для подтверждения) панель открывает
адрес http-монитора как обычный браузер и как Googlebot и разбирает ответ: title, размер,
внешние домены из script/iframe (в том числе внутри своих js) и версии движка и PHP из
заголовков и meta generator.

Признаки:
- сильные, сами по себе повод: главная опустела (размер упал в разы или пропал title),
  Googlebot видит другую страницу;
- слабые, повод только вместе: сменился title, новый внешний домен, движок или PHP с
  закрытой дырой.

Сильный признак или два любых - "похоже на взлом": алерт и красная строка в "Что сломано".
Один слабый - желтая строка, в Telegram тихо. База страницы - первый удачный разбор, дальше
ее меняют только кнопки карточки ("это наше", "новая норма", "Разобрались"). Признак
ставится со второго разбора подряд и снимается сам, когда причина ушла.

Версии с дырами берутся из открытых источников (api.wordpress.org, php.net) раз в сутки и
хранятся в настройках панели: без сети работаем по последнему загруженному, а если не
загружали ни разу - версии просто не проверяем.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import checks as checks_exec

log = logging.getLogger("kervax.tamper")

SCAN_EVERY = timedelta(hours=1)
RECHECK = timedelta(minutes=10)  # подозрение подтверждаем быстро, а не через час
POSTPONE = timedelta(days=7)     # "отложить" для версии с дырой
OLD_GRACE = timedelta(days=2)    # свежее исправление: пару дней на автообновление
VERSIONS_EVERY = timedelta(hours=24)
_LOOK_EVERY = timedelta(minutes=1)
_CONCURRENCY = 4
_MAX_BODY = 2_000_000
_MAX_JS = 15
_MAX_JS_BODY = 1_500_000
_TIMEOUT = 20.0

_BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

STRONG = frozenset({"empty", "bot"})
# порядок в строках и алертах: сильные сначала
ORDER = ("empty", "bot", "title", "dom", "old")
ICON = {"empty": "📄", "bot": "🤖", "title": "📝", "dom": "🌐", "old": "🧩"}

# адреса из документации и пространств имен: встречаются в любом js, к загрузке не относятся
_DOC_HOSTS = frozenset({
    "w3.org", "www.w3.org", "schema.org", "purl.org", "ogp.me", "xmlns.com", "ns.adobe.com",
    "opensource.org", "www.opensource.org", "jquery.org", "fb.me", "reactjs.org", "react.dev",
    "developer.mozilla.org", "mozilla.org", "github.com", "npmjs.com", "www.npmjs.com",
    "momentjs.com", "localhost", "example.com", "www.example.com", "json-schema.org",
})
_URL_LIT = re.compile(
    r"""["'`](?:https?:)?//((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?=[/"'`?#])""",
    re.I,
)
_WP_GEN = re.compile(r"WordPress\s+(\d+\.\d+(?:\.\d+)?)", re.I)
_PHP_VER = re.compile(r"PHP/(\d+)\.(\d+)\.(\d+)", re.I)
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


# ---------- разбор страницы ----------

class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._in_script = False
        self.srcs: list[tuple[str, str]] = []  # (тег, src)
        self.inline: list[str] = []
        self.generator = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and not self.title:
            self._in_title = True
        elif tag in ("script", "iframe") and a.get("src"):
            self.srcs.append((tag, a["src"].strip()))
        elif tag == "meta" and a.get("name", "").lower() == "generator":
            self.generator = self.generator or a.get("content", "")[:120]
        if tag == "script" and not a.get("src"):
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_script and sum(map(len, self.inline)) < _MAX_BODY:
            self.inline.append(data)


def clean_title(t: str) -> str:
    return _SPACES.sub(" ", t or "").strip()[:200]


def norm_title(t: str) -> str:
    """Для сравнения: регистр, пробелы и цифры (дата в title новостного сайта) не в счет."""
    return _DIGITS.sub("0", clean_title(t).lower())


def is_external(host: str, site_host: str) -> bool:
    return checks_exec._registrable_domain(host) != checks_exec._registrable_domain(site_host)


def _ok_host(h: str) -> bool:
    h = h.lower().rstrip(".")
    return bool(h) and h not in _DOC_HOSTS and "." in h


def js_domains(text: str) -> set[str]:
    """Домены из адресов в строковых литералах js (iframe/script, вставленные кодом)."""
    return {m.group(1).lower() for m in _URL_LIT.finditer(text or "") if _ok_host(m.group(1))}


def parse_page(text: str, url: str) -> dict:
    """title, внешние домены страницы {домен: откуда} и свои js для разбора."""
    p = _Page()
    try:
        p.feed(text or "")
        p.close()
    except Exception:  # noqa: BLE001 - кривой html разбираем, сколько получилось
        pass
    host = (urlparse(url).hostname or "").lower()
    domains: dict[str, str] = {}
    own_js: list[str] = []
    for tag, src in p.srcs:
        full = urljoin(url, src)
        u = urlparse(full)
        if u.scheme not in ("http", "https", ""):
            continue
        h = (u.hostname or "").lower()
        if not h or not is_external(h, host):
            if tag == "script" and full not in own_js:
                own_js.append(full)
            continue
        if _ok_host(h):
            domains.setdefault(h, tag)
    for h in js_domains("\n".join(p.inline)):
        if is_external(h, host):
            domains.setdefault(h, "inline")
    return {"title": clean_title(p.title), "domains": domains, "js": own_js[:_MAX_JS],
            "generator": p.generator.strip()}


def engine(headers: dict, generator: str, text: str) -> dict:
    """Что видно снаружи: версия WordPress из meta generator, PHP из заголовков."""
    out: dict = {}
    m = _WP_GEN.search(generator or "")
    if m:
        out["wp"] = m.group(1)
    elif generator:
        out["gen"] = generator
    elif "/bitrix/" in (text or "")[:_MAX_BODY]:
        out["gen"] = "1C-Bitrix"
    for k in ("x-powered-by", "server"):
        m = _PHP_VER.search(str(headers.get(k, "")))
        if m:
            out["php"] = ".".join(m.groups())
            break
    return out


# ---------- версии с дырами ----------

def _vt(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def _branch(v: str) -> str:
    return ".".join(v.split(".")[:2])


def wp_verdict(ver: str, stable: dict) -> dict | None:
    """WordPress с дырой: api.wordpress.org считает версию insecure. fix - первая безопасная
    в той же ветке (7.0 -> 7.0.7), если ветку уже не чинят - последняя вообще."""
    if not ver or not stable or stable.get(ver) != "insecure":
        return None
    br = _branch(ver)
    safe = sorted((v for v, s in stable.items() if s != "insecure" and _vt(v) > _vt(ver)), key=_vt)
    same = [v for v in safe if _branch(v) == br]
    fix = (same or safe[-1:] or [""])[0]
    return {"what": f"WordPress {ver}", "fix": fix}


def php_verdict(ver: str, php: dict, now: datetime) -> dict | None:
    """PHP с дырой: в той же ветке вышел релиз с исправлением безопасности (старше пары дней,
    иначе это просто сегодняшний выпуск), или ветка больше не поддерживается."""
    if not ver or not php:
        return None
    sup = php.get("sup") or []
    br = _branch(ver)
    if sup and br not in sup and _vt(br) < min(_vt(s) for s in sup):
        return {"what": f"PHP {ver}", "eol": True}
    fixes = []
    for v, (date, sec) in (php.get("rel") or {}).items():
        if not sec or _branch(v) != br or _vt(v) <= _vt(ver):
            continue
        try:
            at = datetime.strptime(date, "%d %b %Y").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if now - at >= OLD_GRACE:
            fixes.append(v)
    if not fixes:
        return None
    return {"what": f"PHP {ver}", "fix": max(fixes, key=_vt)}


def old_items(eng: dict, versions: dict, now: datetime) -> list[dict]:
    out = []
    w = wp_verdict(eng.get("wp", ""), versions.get("wp") or {})
    if w:
        out.append(w)
    p = php_verdict(eng.get("php", ""), versions.get("php") or {}, now)
    if p:
        out.append(p)
    return out


# ---------- состояние ----------

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _dt(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def observe(state: dict | None, obs: dict, now: datetime, fleet: set[str], versions: dict) -> dict:
    """Новое состояние по разбору obs. obs: size, title, domains {домен: откуда}, bot (size,
    title) или None, engine. fleet - домены из баз других сайтов (их не считаем чужими)."""
    st = dict(state or {})
    base = st.get("base")
    seen: dict[str, dict] = {}
    if not base:
        base = {"title": obs["title"], "size": obs["size"], "domains": dict(obs["domains"]), "at": _iso(now)}
    else:
        b_size, b_title = int(base.get("size") or 0), base.get("title") or ""
        if (b_size >= 2048 and obs["size"] * 5 <= b_size) or (b_title and not obs["title"]):
            seen["empty"] = {"size": obs["size"], "was": b_size, "title": b_title}
        elif b_title and obs["title"] and norm_title(obs["title"]) != norm_title(b_title):
            seen["title"] = {"title": obs["title"], "was": b_title}
        known = set(base.get("domains") or {}) | fleet
        new = {d: src for d, src in obs["domains"].items() if d not in known}
        if new:
            seen["dom"] = {"list": new}
    bot = obs.get("bot")
    if bot:
        diff_title = norm_title(bot["title"]) != norm_title(obs["title"])
        big, small = max(bot["size"], obs["size"]), max(min(bot["size"], obs["size"]), 1)
        diff_size = big >= 2048 and big >= small * 3
        if (diff_title or diff_size) and norm_title(bot["title"]) != (st.get("bot_ok") or "\0"):
            seen["bot"] = {"title": bot["title"], "size": bot["size"], "human": obs["size"]}
    post = {k: v for k, v in (st.get("post") or {}).items() if (_dt(v) or now) > now}
    old = [] if "old" in post else old_items(obs.get("engine") or {}, versions, now)
    signs = dict(st.get("signs") or {})
    pend = dict(st.get("pend") or {})
    for k in ("empty", "title", "dom", "bot"):
        if k not in seen:
            signs.pop(k, None)
            pend.pop(k, None)
            continue
        if k in signs:
            prev = signs[k]
            if k == "dom":
                # новые домены копятся в тот же признак, у каждого свое "с какого времени"
                lst = {d: v for d, v in (prev.get("list") or {}).items() if d in seen[k]["list"]}
                for d, src in seen[k]["list"].items():
                    lst.setdefault(d, {"src": src, "since": _iso(now)})
                signs[k] = {**prev, "list": lst}
            else:
                signs[k] = {**seen[k], "since": prev.get("since") or _iso(now)}
        elif k in pend:
            since = pend.pop(k)
            if k == "dom":
                signs[k] = {"list": {d: {"src": s, "since": since} for d, s in seen[k]["list"].items()},
                            "since": since}
            else:
                signs[k] = {**seen[k], "since": since}
        else:
            pend[k] = _iso(now)
    if old:
        signs["old"] = {"items": old, "since": (signs.get("old") or {}).get("since") or _iso(now)}
    else:
        signs.pop("old", None)
    # спокойная главная понемногу подтягивает базу размера: сайт растет, база за ним
    if "empty" not in seen and "empty" not in pend and base.get("size") and obs["size"]:
        base = {**base, "size": int(base["size"] * 0.8 + obs["size"] * 0.2)}
    st.update(base=base, signs=signs, pend=pend, post=post, engine=obs.get("engine") or {},
              last={"at": _iso(now), "size": obs["size"], "title": obs["title"],
                    "domains": obs["domains"], "bot": bot},
              next=_iso(now + (RECHECK if pend else SCAN_EVERY)))
    st.pop("err", None)
    return st


def failed(state: dict | None, why: str, now: datetime) -> dict:
    """Разобрать не удалось (не html, не 200, сеть): признаки не трогаем, пробуем через час."""
    st = dict(state or {})
    st.update(err=why[:200], next=_iso(now + SCAN_EVERY))
    return st


def level(state: dict | None) -> str:
    signs = (state or {}).get("signs") or {}
    if not signs:
        return "none"
    if STRONG & set(signs) or len(signs) >= 2:
        return "hack"
    return "one"


def _pl(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "признак"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "признака"
    return "признаков"


def _size(b: int) -> str:
    from app import tg_fmt
    if b >= 1024:
        return tg_fmt.size(b)
    return f"{b} " + ("байта" if b % 10 in (2, 3, 4) and not 12 <= b % 100 <= 14 else "байт")


def phrase(key: str, s: dict) -> str:
    """Короткая фраза признака: для строки на главной и алерта."""
    if key == "empty":
        return "главная пустая" if not s.get("title") or s.get("size", 0) < 1024 else "главная опустела"
    if key == "bot":
        return "Googlebot видит другое"
    if key == "title":
        return f'title сменился на "{s.get("title", "")[:40]}"'
    if key == "dom":
        lst = sorted(s.get("list") or {})
        if len(lst) == 1:
            return f"чужой домен {lst[0]}"
        from app import tg_fmt
        return "чужие домены " + tg_fmt.names(lst, 2)
    if key == "old":
        it = (s.get("items") or [{}])[0]
        return f"{it.get('what', '')} " + ("без поддержки" if it.get("eol") else "с дырой")
    return key


def _sorted(signs: dict) -> list[str]:
    return sorted(signs, key=lambda k: ORDER.index(k) if k in ORDER else 99)


def summary(state: dict | None) -> str:
    """Строка "Что сломано": что именно, без счетчиков."""
    signs = (state or {}).get("signs") or {}
    keys = _sorted(signs)
    if not keys:
        return ""
    parts = [phrase(k, signs[k]) for k in keys]
    text = ", ".join(parts[:3]) + (f" и еще {len(parts) - 3}" if len(parts) > 3 else "")
    return f"похоже на взлом: {text}" if level(state) == "hack" else text


def alert_text(state: dict) -> str:
    """Что случилось и 1-2 строки конкретики для алерта (строки через \\n)."""
    from app import tg_fmt
    signs = state.get("signs") or {}
    keys = _sorted(signs)
    n = len(keys)
    strong = [k for k in keys if k in STRONG]
    weak = [k for k in keys if k not in STRONG]
    lines = [f"Похоже на взлом: {n} {_pl(n)}"]
    if strong:
        lines.append(tg_fmt.cap(", ".join(_detail(k, signs[k]) for k in strong)))
        if weak:
            lines.append(tg_fmt.cap(", ".join(phrase(k, signs[k]) for k in weak)))
    else:
        lines.append(tg_fmt.cap(_detail(weak[0], signs[weak[0]])))
        if weak[1:]:
            lines.append(tg_fmt.cap(", ".join(phrase(k, signs[k]) for k in weak[1:])))
    return "\n".join(lines)


def _detail(key: str, s: dict) -> str:
    if key == "empty":
        return f"главная {_size(int(s.get('size') or 0))} вместо {_size(int(s.get('was') or 0))}"
    if key == "bot":
        return f'Googlebot видит "{s.get("title", "")[:40]}"' if s.get("title") else "Googlebot видит другое"
    if key == "dom":
        lst = s.get("list") or {}
        if len(lst) == 1:
            d, v = next(iter(lst.items()))
            return f"чужой домен {d} {_where(v.get('src', ''))}".strip()
    if key == "old":
        it = (s.get("items") or [{}])[0]
        if it.get("fix"):
            return f"{it['what']} с дырой, исправлено в {it['fix']}"
    return phrase(key, s)


def _where(src: str) -> str:
    if src in ("script", "iframe"):
        return f"в {src} на странице"
    if src == "inline":
        return "в скрипте на странице"
    return f"в {src.rsplit('/', 1)[-1]}" if src else ""


def view(state: dict | None) -> dict | None:
    """Для карточки и главной: уровень, строки признаков с конкретикой, что видно о сайте."""
    if not state:
        return None
    signs = state.get("signs") or {}
    rows = []
    for k in _sorted(signs):
        s = signs[k]
        if k == "dom":
            for d, v in sorted((s.get("list") or {}).items()):
                rows.append({"key": "dom", "arg": d, "strong": False, "since": v.get("since"),
                             "domain": d, "src": v.get("src", "")})
            continue
        if k == "old":
            for it in s.get("items") or []:
                rows.append({"key": "old", "arg": "", "strong": False, "since": s.get("since"), **it})
            continue
        rows.append({"key": k, "arg": "", "strong": k in STRONG, "since": s.get("since"),
                     **{x: s[x] for x in ("size", "was", "title", "human") if x in s}})
    since = min((r["since"] for r in rows if r.get("since")), default=None)
    base = state.get("base") or {}
    last = state.get("last") or {}
    return {
        "level": level(state), "summary": summary(state), "since": since, "signs": rows,
        "base": {"title": base.get("title", ""), "size": base.get("size")},
        "last_at": last.get("at"), "size": last.get("size"), "engine": state.get("engine") or {},
        "domains": sorted(base.get("domains") or {}), "err": state.get("err", ""),
    }


def accept(state: dict, key: str, arg: str, now: datetime) -> dict:
    """Кнопки карточки: "это наше" (домен в базу), "новая норма" (страница в базу), "это норм"
    (разница для Googlebot), "отложить" (версия), "all" - Разобрались: все текущее как норма."""
    st = json.loads(json.dumps(state or {}))
    base = st.setdefault("base", {})
    last = st.get("last") or {}
    signs = st.setdefault("signs", {})
    pend = st.setdefault("pend", {})
    keys = list(signs) + list(pend) if key == "all" else [key]
    for k in keys:
        if k in ("empty", "title"):
            if last:
                base.update(title=last.get("title", ""), size=last.get("size", 0))
            for x in ("empty", "title"):
                signs.pop(x, None)
                pend.pop(x, None)
        elif k == "dom":
            doms = base.setdefault("domains", {})
            lst = (signs.get("dom") or {}).get("list") or {}
            take = [arg] if (key != "all" and arg) else list(lst) + list((last.get("domains") or {}))
            for d in take:
                doms[d] = (lst.get(d) or {}).get("src") or (last.get("domains") or {}).get(d, "")
            left = {d: v for d, v in lst.items() if d not in doms}
            if left:
                signs["dom"]["list"] = left
            else:
                signs.pop("dom", None)
                pend.pop("dom", None)
        elif k == "bot":
            if last.get("bot"):
                st["bot_ok"] = norm_title(last["bot"].get("title", ""))
            signs.pop("bot", None)
            pend.pop("bot", None)
        elif k == "old":
            st.setdefault("post", {})["old"] = _iso(now + POSTPONE)
            signs.pop("old", None)
    return st


# ---------- сеть ----------

async def _get(client: httpx.AsyncClient, url: str, ua: str, limit: int = _MAX_BODY):
    r = await client.get(url, headers={"User-Agent": ua})
    return r, r.text[:limit] if r.content else ""


def _is_html(r: httpx.Response) -> bool:
    return "html" in r.headers.get("content-type", "").lower()


async def scan(check) -> dict:
    """Разбор монитора: {"obs": ...} или {"err": ...}."""
    raw = check.target.strip()
    url = raw if urlparse(raw).scheme else "https://" + raw
    auth = (httpx.BasicAuth(check.auth_user or "", check.auth_pass or "")
            if getattr(check, "auth_method", "") == "basic" else None)
    headers = checks_exec._parse_headers(getattr(check, "http_headers", "") or "")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, auth=auth, headers=headers,
                                 verify=not getattr(check, "ignore_tls", False)) as client:
        try:
            r, text = await _get(client, url, checks_exec._USER_AGENT)
        except httpx.HTTPError as exc:
            return {"err": f"не открылась: {type(exc).__name__}"}
        if not 200 <= r.status_code < 300:
            return {"err": f"ответ {r.status_code}"}
        if not _is_html(r):
            return {"err": "не html"}
        final = str(r.url)
        page = parse_page(text, final)
        domains = dict(page["domains"])
        host = (urlparse(final).hostname or "").lower()
        for js in page["js"]:
            try:
                jr, jt = await _get(client, js, checks_exec._USER_AGENT, _MAX_JS_BODY)
            except httpx.HTTPError:
                continue
            if jr.status_code != 200:
                continue
            for d in js_domains(jt):
                if is_external(d, host):
                    domains.setdefault(d, urlparse(js).path or js)
        bot = None
        try:
            br, bt = await _get(client, url, _BOT_UA)
            # Cloudflare и др. режут поддельный Googlebot (403, проверка): это не подмена
            if 200 <= br.status_code < 300 and _is_html(br):
                bot = {"size": len(br.content), "title": parse_page(bt, str(br.url))["title"]}
        except httpx.HTTPError:
            pass
        return {"obs": {"size": len(r.content), "title": page["title"], "domains": domains, "bot": bot,
                        "engine": engine(r.headers, page["generator"], text)}}


async def _fetch_versions() -> dict:
    out: dict = {}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                 headers={"User-Agent": checks_exec._USER_AGENT}) as c:
        try:
            r = await c.get("https://api.wordpress.org/core/stable-check/1.0/")
            if r.status_code == 200:
                out["wp"] = {str(k): str(v) for k, v in r.json().items()}
        except (httpx.HTTPError, ValueError):
            log.warning("версии WordPress не загрузились", exc_info=True)
        try:
            rel = (await c.get("https://www.php.net/releases/index.php?json&version=8&max=1000")).json()
            cur = (await c.get("https://www.php.net/releases/index.php?json&version=8")).json()
            out["php"] = {
                "rel": {v: [d.get("date", ""), "security" in (d.get("tags") or [])] for v, d in rel.items()
                        if isinstance(d, dict)},
                "sup": [str(x) for x in cur.get("supported_versions") or []],
            }
        except (httpx.HTTPError, ValueError, AttributeError):
            log.warning("версии PHP не загрузились", exc_info=True)
    return out


_VKEY = "tamper_versions"


async def versions(session: AsyncSession, now: datetime, refresh: bool = True) -> dict:
    """Версии с дырами: из настроек, раз в сутки обновляем; без сети - старые."""
    from app import settings_store
    try:
        have = json.loads(await settings_store.get_raw(session, _VKEY) or "{}")
    except ValueError:
        have = {}
    at = _dt(have.get("at"))
    if refresh and (at is None or now - at >= VERSIONS_EVERY):
        got = await _fetch_versions()
        # не загрузилось - оставляем старое, но повторим не раньше, чем через сутки
        have = {**have, **got, "at": _iso(now)}
        await settings_store.set_raw(session, _VKEY, json.dumps(have))
    return have


# ---------- фоновый прогон ----------

_running: asyncio.Task | None = None
_last_start: datetime | None = None


def due(check, now: datetime) -> bool:
    if check.type != "http" or not check.enabled or check.probe_local:
        return False
    if check.last_status not in ("up", "degraded"):
        return False  # лежит - это другой алерт; разбирать нечего
    nxt = _dt((check.tamper_state or {}).get("next"))
    return nxt is None or nxt <= now


def fleet_domains(rows, skip_id: int) -> set[str]:
    out: set[str] = set()
    for cid, st in rows:
        if cid != skip_id and isinstance(st, dict):
            out |= set(((st.get("base") or {}).get("domains") or {}))
    return out


async def _scan_due(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    from app.models import Check
    async with session_factory() as s:
        todo = [c for c in await s.scalars(select(Check).where(Check.type == "http")) if due(c, now)]
        if not todo:
            return 0
        vers = await versions(s, now)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(c):
        async with sem:
            try:
                return c.id, await scan(c)
            except Exception as exc:  # noqa: BLE001 - один сайт не роняет прогон
                log.warning("признаки взлома: %s не разобран", c.target, exc_info=True)
                return c.id, {"err": f"ошибка разбора: {type(exc).__name__}"}

    got = await asyncio.gather(*(one(c) for c in todo))
    async with session_factory() as s:
        states = (await s.execute(select(Check.id, Check.tamper_state).where(Check.type == "http"))).all()
        for cid, res in got:
            row = await s.get(Check, cid)
            if row is None:
                continue
            t = datetime.now(timezone.utc)
            if "obs" in res:
                row.tamper_state = observe(row.tamper_state, res["obs"], t, fleet_domains(states, cid), vers)
            else:
                row.tamper_state = failed(row.tamper_state, res["err"], t)
        await s.commit()
    return len(got)


async def start_scan(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Разбор тех, кому пора, фоном: тик планировщика его не ждет."""
    global _running, _last_start
    if _running is not None and not _running.done():
        return
    if _last_start is not None and now - _last_start < _LOOK_EVERY:
        return
    _last_start = now

    async def run() -> None:
        try:
            n = await _scan_due(session_factory, now)
            if n:
                log.info("признаки взлома: разобрано сайтов %d", n)
        except Exception:  # noqa: BLE001 - фоновая задача не должна ронять планировщик
            log.exception("разбор признаков взлома упал")

    _running = asyncio.create_task(run())


# ---------- алерты ----------

def alert_due(state: dict | None, sent: list | None) -> tuple[str, list] | None:
    """("tamper" | "tamperrec", что запомнить после отправки) или None.

    Похоже на взлом - алерт; потом новый только при новом сильном признаке. Признаков не
    осталось - отбой. Остался один слабый - молча забываем, следующий взлом снова алертит."""
    lv = level(state)
    sent = list(sent or [])
    signs = (state or {}).get("signs") or {}
    if lv == "hack":
        keys = sorted({"hack", *(k for k in signs if k in STRONG)})
        if "hack" not in sent or set(keys) - set(sent):
            return "tamper", sorted(set(keys) | set(sent))
        return None
    if sent:
        return ("tamperrec", []) if lv == "none" else ("", [])
    return None
