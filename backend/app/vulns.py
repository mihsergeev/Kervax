"""Важные уязвимости в пакетах серверов: инвентарь пакетов (app/packages.py) против базы Canonical.

Только данные - как и пакеты: ни алертов, ни пунктов "требует действий". И только важное:
приоритет Ubuntu critical либо CVE из каталога CISA KEV (уязвимость уже эксплуатируют). High,
средние и низкие не показываем вовсе: high у ядра - сотни на любом сервере, и за ними не видно
главного. И только то, что закрывается обновлением: исправления еще нет - сделать с ним нечего.

База - OVAL Canonical по пакетам (com.ubuntu.<релиз>.pkg.oval.xml.bz2, 1-2 МБ на релиз): по
исходному пакету список CVE с приоритетом, версия с исправлением и имена бинарных пакетов, по
которым его ищут. Поэтому агенту хватает того, что он уже шлет: имя и версия бинарного пакета.
Ядро проверяется по работающему (uname из os_upd.kernel), а не по пакетам: новое ядро без
перезагрузки уязвимость не закрывает. Исправление с ~esm в версии выходит только в Ubuntu Pro.

Панель скачивает базы раз в сутки для релизов, которые есть в парке, вместе с каталогом KEV, и
кладет в таблицу vuln_feeds уже отобранное: только важные CVE, на релиз сотни КБ.
"""
from __future__ import annotations

import asyncio
import bz2
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import IO

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Server, ServerPackages, VulnFeed

log = logging.getLogger(__name__)

OVAL_URL = "https://security-metadata.canonical.com/oval/com.ubuntu.{rel}.pkg.oval.xml.bz2"
# зеркало на GitHub первым: сам cisa.gov из части дата-центров отвечает 403
KEV_URLS = (
    "https://raw.githubusercontent.com/cisagov/kev-data/main/known_exploited_vulnerabilities.json",
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
)

# версия Ubuntu -> кодовое имя релиза в базе Canonical
RELEASES = {
    "16.04": "xenial", "18.04": "bionic", "20.04": "focal", "22.04": "jammy", "24.04": "noble",
    "24.10": "oracular", "25.04": "plucky", "25.10": "questing", "26.04": "resolute",
}
# когда кончилась обычная поддержка: дальше исправления выходят только в Ubuntu Pro
EOL = {"xenial": "2021-04", "bionic": "2023-05", "focal": "2025-05"}
IMPORTANT = ("critical",)
_PRIO_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "negligible": 4}
# исходники ядер под другую архитектуру: суффикс у них тот же (-generic), отличить по uname нельзя
_ARCH_KERNEL = re.compile(r"riscv|raspi|allwinner|starfive|xilinx|armadaxp|ti-omap|nvidia-tegra|bluefield")

REFRESH_EVERY = timedelta(hours=24)
RETRY_AFTER = timedelta(hours=1)
_LOOK_EVERY = timedelta(minutes=10)


def release_of(os_name: str) -> str:
    """Кодовое имя релиза Ubuntu по строке ОС из отчета ("Ubuntu 24.04.3 LTS" -> noble)."""
    m = re.match(r"\s*Ubuntu\s+(\d+\.\d+)", os_name or "")
    return RELEASES.get(m.group(1), "") if m else ""


# ---------------------------------------------------------------- версии dpkg

def _order(c: str) -> int:
    if not c or c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    if c == "~":
        return -1
    return ord(c) + 256


def _verrevcmp(a: str, b: str) -> int:
    """Сравнение частей версии как в dpkg (lib/dpkg/version.c, verrevcmp)."""
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        first_diff = 0
        while (i < la and not a[i].isdigit()) or (j < lb and not b[j].isdigit()):
            ac = _order(a[i] if i < la else "")
            bc = _order(b[j] if j < lb else "")
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < la and a[i] == "0":
            i += 1
        while j < lb and b[j] == "0":
            j += 1
        while i < la and j < lb and a[i].isdigit() and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < la and a[i].isdigit():
            return 1
        if j < lb and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split(v: str) -> tuple[int, str, str]:
    v = (v or "").strip()
    epoch = 0
    if ":" in v:
        e, rest = v.split(":", 1)
        if e.isdigit():
            epoch, v = int(e), rest
    up, sep, rev = v.rpartition("-")
    if not sep:
        up, rev = v, ""
    return epoch, up, rev


def vercmp(a: str, b: str) -> int:
    """<0, 0, >0 - как dpkg --compare-versions"""
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return ea - eb
    return _verrevcmp(ua, ub) or _verrevcmp(ra, rb)


# ---------------------------------------------------------------- разбор базы

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_oval(fh: IO[bytes], kev: set[str]) -> dict:
    """Отбор из OVAL Canonical (pkg) только важного.

    {"pkgs": {бинарный пакет: [[исходный, исправлено в, [[CVE, приоритет, cvss], ...]], ...]},
     "kernel": [[исходный, regex uname, исправлено в (ABI), [[CVE, приоритет, cvss], ...]], ...],
     "kev": [CVE из KEV, что есть в базе], "gen": когда собрана база}

    Строки базы по исходному пакету: у CVE ссылка на тест, тест - на объект (имена бинарных
    пакетов) и состояние ("версия меньше X"). У ядра тест версии идет в паре с тестом uname в
    одной группе AND: какое ядро работает (regex по uname -r) и какая у него версия."""
    defs: list[tuple[str, list[tuple[str, str, float, str]], list[tuple[str, str]]]] = []
    tests: dict[str, tuple[str, str, str]] = {}   # id -> (вид, объект, состояние)
    objects: dict[str, str] = {}                 # id -> переменная с именами пакетов
    states: dict[str, str] = {}                  # id -> версия с исправлением или regex uname
    variables: dict[str, list[str]] = {}
    gen = ""
    for _ev, el in ET.iterparse(fh, events=("end",)):
        tag = _local(el.tag)
        if tag == "timestamp" and not gen:
            gen = (el.text or "").strip()
        elif tag == "definition":
            if el.get("class") == "vulnerability":
                title = ""
                cves: list[tuple[str, str, float, str]] = []
                for c in el.iter():
                    t = _local(c.tag)
                    if t == "title":
                        title = (c.text or "").strip()
                    elif t == "cve":
                        prio = (c.get("priority") or "").lower()
                        cid = (c.text or "").strip()
                        if prio in IMPORTANT or cid in kev:
                            try:
                                cvss = float(c.get("cvss_score") or 0)
                            except ValueError:
                                cvss = 0.0
                            cves.append((cid, prio, cvss, c.get("test_ref") or ""))
                pairs: list[tuple[str, str]] = []
                if cves:
                    for grp in el.iter():
                        if _local(grp.tag) != "criteria" or grp.get("operator") != "AND":
                            continue
                        refs = [c.get("test_ref") or "" for c in grp if _local(c.tag) == "criterion"]
                        if len(refs) == 2:
                            pairs.append((refs[0], refs[1]))
                    defs.append((title, cves, pairs))
            el.clear()
        elif tag.endswith("_test") and el.get("id"):
            obj = st = ""
            for c in el:
                if _local(c.tag) == "object":
                    obj = c.get("object_ref") or ""
                elif _local(c.tag) == "state":
                    st = c.get("state_ref") or ""
            tests[el.get("id") or ""] = (tag, obj, st)
            el.clear()
        elif tag == "dpkginfo_object" and el.get("id"):
            for c in el:
                if _local(c.tag) == "name":
                    objects[el.get("id") or ""] = c.get("var_ref") or ""
            el.clear()
        elif tag in ("dpkginfo_state", "variable_state", "uname_state") and el.get("id"):
            for c in el:
                if _local(c.tag) in ("evr", "value", "os_release"):
                    states[el.get("id") or ""] = (c.text or "").strip()
            el.clear()
        elif tag == "constant_variable" and el.get("id"):
            variables[el.get("id") or ""] = [(v.text or "").strip() for v in el if _local(v.tag) == "value"]
            el.clear()

    pkgs: dict[str, list] = {}
    kernel: list = []
    seen_kev: set[str] = set()
    for src, cves, pairs in defs:
        by_test: dict[str, list] = {}
        for cid, prio, cvss, tref in cves:
            by_test.setdefault(tref, []).append([cid, prio, cvss])
        for tref, items in by_test.items():
            kind, obj, st = tests.get(tref, ("", "", ""))
            fixed = states.get(st, "")
            if not fixed:
                continue  # исправления нет - обновлением не закрыть
            if kind == "dpkginfo_test":
                names = variables.get(objects.get(obj, ""), [])
                for b in names:
                    pkgs.setdefault(b, []).append([src, fixed, items])
                if not names:
                    continue
            elif kind == "variable_test":
                rx = ""
                for un, vt in pairs:
                    if vt == tref:
                        rx = states.get(tests.get(un, ("", "", ""))[2], "")
                        if rx:
                            break
                if not rx:
                    continue
                kernel.append([src, rx, fixed.split(":", 1)[-1], items])
            else:
                continue
            seen_kev.update(c[0] for c in items if c[0] in kev)
    return {"pkgs": pkgs, "kernel": kernel, "kev": sorted(seen_kev), "gen": gen}


def parse_kev(raw: bytes) -> set[str]:
    try:
        d = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(d, dict):
        return set()
    return {str(v.get("cveID") or "") for v in d.get("vulnerabilities") or [] if isinstance(v, dict)} - {""}


def load_oval(raw: bytes, kev: set[str]) -> dict:
    """bz2 распаковывается потоком: целиком XML релиза - сотня МБ"""
    with bz2.BZ2File(io.BytesIO(raw)) as fh:
        return parse_oval(fh, kev)


# ---------------------------------------------------------------- сверка

@lru_cache(maxsize=4096)
def _rx(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _cves(items: list, kev: set[str], seen: set[str]) -> list[dict]:
    out = []
    for cid, prio, cvss in items:
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "prio": prio, "cvss": cvss, "kev": cid in kev})
    return out


def _sort_key(f: dict):
    best = min((_PRIO_RANK.get(c["prio"], 9) - (10 if c["kev"] else 0) for c in f["cves"]), default=9)
    return (best, -max((c["cvss"] for c in f["cves"]), default=0), f["src"])


def pro_attached(items: list) -> bool:
    """Подключен ли Ubuntu Pro: стоят пакеты из ESM (~esm в версии)"""
    return any(isinstance(it, (list, tuple)) and len(it) > 1 and "esm" in str(it[1]) for it in items or [])


def match(feed: dict | None, items: list, kernel: str) -> list[dict]:
    """Важные уязвимости сервера, по исходному пакету одна строка, ядро - одна строка.

    [{"src", "pkgs": [[бинарный, установлено]], "fixed", "pro": исправление только в Ubuntu Pro,
      "kernel": ядро (закрывается обновлением и перезагрузкой), "cves": [{id, prio, cvss, kev}]}]"""
    if not feed:
        return []
    kev = set(feed.get("kev") or [])
    by_src: dict[str, dict] = {}
    idx = feed.get("pkgs") or {}
    for it in items or []:
        if not isinstance(it, (list, tuple)) or len(it) < 2:
            continue
        name, ver = str(it[0]), str(it[1])
        for src, fixed, cves in idx.get(name, ()):
            if vercmp(ver, fixed) >= 0:
                continue
            f = by_src.setdefault(src, {"src": src, "pkgs": [], "fixed": fixed, "kernel": False,
                                        "cves": [], "_seen": set()})
            if [name, ver] not in f["pkgs"]:
                f["pkgs"].append([name, ver])
            if vercmp(fixed, f["fixed"]) > 0:
                f["fixed"] = fixed
            f["cves"] += _cves(cves, kev, f["_seen"])
    m = re.match(r"^([\d.]+-\d+)[-\w]+$", kernel or "")
    if m:
        run = m.group(1)
        flavor = kernel[len(run):]
        hits = []
        for src, rx, fixed, cves in feed.get("kernel") or ():
            # ядро под другую архитектуру с тем же суффиксом (-generic у riscv) - только если суффикс
            # работающего ядра сам говорит об этой архитектуре (-raspi)
            if _ARCH_KERNEL.search(src) and not _ARCH_KERNEL.search(flavor):
                continue
            p = _rx(rx)
            if p is not None and p.match(kernel):
                hits.append((src, fixed, cves))
        for src, fixed, cves in hits:
            if vercmp(run, fixed) >= 0:
                continue
            f = by_src.get("\0kernel")
            if f is None:
                f = by_src["\0kernel"] = {"src": src, "pkgs": [["uname", kernel]], "fixed": fixed,
                                          "kernel": True, "cves": [], "_seen": set()}
            if vercmp(fixed, f["fixed"]) > 0:
                f["fixed"] = fixed
            if len(src) < len(f["src"]):
                f["src"] = src
            f["cves"] += _cves(cves, kev, f["_seen"])
    out = []
    for f in by_src.values():
        f.pop("_seen", None)
        f["pro"] = "esm" in f["fixed"]
        f["cves"].sort(key=lambda c: (not c["kev"], _PRIO_RANK.get(c["prio"], 9), -c["cvss"], c["id"]))
        out.append(f)
    out.sort(key=_sort_key)
    return out


def summary(found: list[dict]) -> dict:
    cves = {c["id"]: c for f in found for c in f["cves"]}
    return {"pkgs": len(found), "cves": len(cves),
            "critical": sum(1 for c in cves.values() if c["prio"] == "critical"),
            "kev": sum(1 for c in cves.values() if c["kev"]),
            "pro": sum(1 for f in found if f["pro"]),
            "kernel": any(f["kernel"] for f in found)}


# ---------------------------------------------------------------- базы в панели

_cache: dict[str, tuple[str, dict]] = {}   # релиз -> (ts строки, данные)


async def feed_of(session: AsyncSession, rel: str) -> tuple[VulnFeed | None, dict | None]:
    """Строка базы релиза и разобранные данные. Данные держатся в памяти, пока строка не обновится."""
    if not rel:
        return None, None
    row = await session.get(VulnFeed, rel)
    if row is None or not row.data:
        return row, None
    stamp = row.ts.isoformat() if row.ts else ""
    hit = _cache.get(rel)
    if hit and hit[0] == stamp:
        return row, hit[1]
    data = row.data if isinstance(row.data, dict) else None
    if data is not None:
        _cache[rel] = (stamp, data)
    return row, data


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite отдает время без пояса, Postgres - с поясом"""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


async def for_server(session: AsyncSession, s: Server, row: ServerPackages | None = None) -> dict:
    """Что показывает карточка сервера"""
    rel = release_of(s.os or "")
    if row is None:
        row = await session.get(ServerPackages, s.id)
    items = (row.items if row is not None else None) or []
    out = {"release": rel, "os": s.os or "", "eol": EOL.get(rel, ""), "pkgs": len(items),
           "pkgs_ts": _iso(row.ts) if row is not None else None,
           "feed_ts": None, "feed_gen": "", "feed_err": "", "pro_on": pro_attached(items),
           "found": [], "summary": summary([])}
    if not rel:
        return out
    frow, feed = await feed_of(session, rel)
    if frow is not None:
        out.update(feed_ts=_iso(frow.ts), feed_gen=frow.gen or "", feed_err=frow.err or "")
    if feed is None or not items:
        return out
    kernel = str(((s.last_report or {}).get("os_upd") or {}).get("kernel") or "")
    found = match(feed, items, kernel)
    out.update(found=found, summary=summary(found))
    return out


async def _fleet_releases(session: AsyncSession) -> set[str]:
    rows = (await session.execute(select(Server.os).where(Server.enabled.is_(True)))).all()
    return {r for r in (release_of(x[0] or "") for x in rows) if r}


async def _get(client: httpx.AsyncClient, url: str) -> bytes:
    r = await client.get(url)
    r.raise_for_status()
    return r.content


async def refresh(session_factory: async_sessionmaker[AsyncSession], releases: list[str],
                  now: datetime) -> dict[str, str]:
    """Скачать и разобрать базы релизов. Возвращает ошибки по релизам; старые данные при
    ошибке остаются - лучше вчерашняя база, чем никакой."""
    errors: dict[str, str] = {}
    kev: set[str] | None = None
    async with httpx.AsyncClient(timeout=120, follow_redirects=True,
                                 headers={"User-Agent": "Kervax vulnerability feed"}) as client:
        for url in KEV_URLS:
            try:
                kev = parse_kev(await _get(client, url)) or None
            except Exception as e:  # noqa: BLE001 - пробуем следующий адрес
                log.warning("каталог KEV не скачался (%s): %s", url, e)
            if kev:
                break
        if not kev:
            # без свежего каталога - тот, что уже в базах: отметки "эксплуатируется" не пропадут
            async with session_factory() as session:
                kev = {c for r in (await session.scalars(select(VulnFeed))).all()
                       if isinstance(r.data, dict) for c in r.data.get("kev") or []}
        for rel in releases:
            try:
                raw = await _get(client, OVAL_URL.format(rel=rel))
                data = await asyncio.to_thread(load_oval, raw, kev)
                err = ""
            except Exception as e:  # noqa: BLE001 - остальные релизы обновятся
                data, err = None, f"{type(e).__name__}: {e}"[:300]
                errors[rel] = err
                log.warning("база уязвимостей %s не обновилась: %s", rel, err)
            async with session_factory() as session:
                row = await session.get(VulnFeed, rel)
                if row is None:
                    row = VulnFeed(release=rel)
                    session.add(row)
                if data is not None:
                    row.data, row.gen, row.ts = data, str(data.get("gen") or "")[:40], now
                row.err, row.tried = err, now
                await session.commit()
    return errors


_running: asyncio.Task | None = None
_last_look: datetime | None = None


async def due_releases(session: AsyncSession, now: datetime) -> list[str]:
    rels = await _fleet_releases(session)
    rows = {r.release: r for r in (await session.scalars(select(VulnFeed))).all()}
    due = []
    for rel in sorted(rels):
        r = rows.get(rel)
        ts, tried = (_aware(r.ts), _aware(r.tried)) if r is not None else (None, None)
        fresh = ts is not None and now - ts < REFRESH_EVERY
        retry_wait = r is not None and bool(r.err) and tried is not None and now - tried < RETRY_AFTER
        if not fresh and not retry_wait:
            due.append(rel)
    return due


async def start_refresh(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Раз в 10 минут посмотреть, каким релизам пора обновить базу, и обновить фоном: тик
    планировщика загрузку не ждет."""
    global _running, _last_look
    if _running is not None and not _running.done():
        return
    if _last_look is not None and now - _last_look < _LOOK_EVERY:
        return
    _last_look = now
    async with session_factory() as session:
        due = await due_releases(session, now)
    if not due:
        return

    async def run() -> None:
        try:
            errs = await refresh(session_factory, due, now)
            log.info("базы уязвимостей обновлены: %s", ", ".join(r for r in due if r not in errs) or "-")
        except Exception:  # noqa: BLE001 - фоновая задача не должна ронять планировщик
            log.exception("обновление баз уязвимостей упало")

    _running = asyncio.create_task(run())
