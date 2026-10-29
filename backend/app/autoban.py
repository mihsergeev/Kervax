"""Автобан сканеров (helper autoban-setup 0.1).

Сканеры, пришедшие за новым сертификатом по логам CT, перебирают /.env, /.git/config, дампы *.sql,
/actuator/heapdump, /@vite/env и подобное. Банит сама нода: helper раз в минуту читает access-логи
(те же, что считает webserver-setup), и адрес, за 10 минут ударивший в 3+ разных пути-ловушки,
получает бан на 80/443 в своей таблице nftables: сутки, повтор за 30 дней - неделю, дальше месяц.
Панель файрвол нод не трогает: она только снимает бан, переключает режим (on, watch, off) и отдает
ноде свой белый список - через спул, как кнопки units-setup. Забанить адрес панель не может.

helper шлет report.d/autoban.json: режим, берет ли файрвол баны, активные баны (за что, где, до
когда, приходил ли адрес после бана), адреса из белого списка, которые били в ловушки, адреса,
пощаженные как прокси, и баны своих адресов, которые helper сам снял.

Где бан на ноде не работает, панель пишет прямо (notes): сайты за Cloudflare (посетитель приходит
с адреса края CF, бан реального адреса ничего не режет - только Cloudflare WAF), край без
access-лога (Caddy, Envoy, Traefik, HAProxy: логов с адресами клиентов нет), в логах только
внутренние адреса, нет nftables.

Алерт autoban - только то, на что надо реагировать: бан своего адреса, включен и не может банить
(10 минут подряд), бан не действует (забаненные снова приходят), лавина 30+ банов за час. Сами
баны - сводкой раз в сутки на всю панель (или раз в час, или выключено) из таблицы autoban_bans.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from zoneinfo import ZoneInfo

from app import alerts, geoip, settings_store, tg_fmt
from app.models import AutobanBan, BackupCommand, Location, Server

STALE = 900  # helper пишет раз в минуту: блок старше 15 минут - helper не работает
MUTE_PREFIX = "autoban:"
SETTINGS_KEY = "autoban"
DIGEST_KEY = "autoban_digest"
AGENT_MIN = "2.55"  # первая версия агента с командой autoban
CANT_HOLD = 600  # "не может банить" - алерт после 10 минут подряд
AVALANCHE_HOUR = 30  # банов за час на сервер - лавина
NOEFFECT_MIN = 3  # из стольких банов с файрволом за сутки
DIGEST_HOUR = 9  # сводка раз в сутки - в 9 утра по часовому поясу алертов
DIGEST_MODES = ("day", "hour", "off")
SYNC_EVERY = 600  # белый список панели на ноду - не чаще раза в 10 минут
_EDGE_NO_LOG = ("Caddy", "Envoy", "Traefik", "HAProxy")
_ENDS = ("expired", "unban", "gone")


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def block(rep: dict | None, now: datetime) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("autoban")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    if now.timestamp() - _num(b.get("ts")) > STALE:
        return None
    return b


def _ver(v: object) -> tuple[int, ...]:
    out = []
    for part in str(v or "").split("."):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


def agent_ok(srv: Server) -> bool:
    """Агент умеет команду autoban (кнопки разбана и режима)."""
    return _ver(srv.agent_version) >= _ver(AGENT_MIN)


def _list(b: dict, key: str) -> list[dict]:
    v = b.get(key)
    return [x for x in v if isinstance(x, dict) and x.get("ip")] if isinstance(v, list) else []


def _ts(v) -> datetime | None:
    x = _num(v)
    return datetime.fromtimestamp(x, timezone.utc) if x > 0 else None


def _paths(x: dict) -> list[str]:
    return [str(p)[:100] for p in x.get("paths") or [] if str(p or "").strip()][:3]


def why_text(x: dict) -> str:
    """'/.env, /.git/config +12 - api.example.com, 41 удар'"""
    p = ", ".join(_paths(x))
    more = int(_num(x.get("more")))
    if more:
        p += f" +{more}"
    where = str(x.get("where") or "")
    hits = int(_num(x.get("hits")))
    tail = ", ".join(s for s in (where, f"{hits} {_hits_word(hits)}" if hits else "") if s)
    return f"{p} - {tail}" if tail else p


def _hits_word(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "ударов"
    return {1: "удар", 2: "удара", 3: "удара", 4: "удара"}.get(n % 10, "ударов")


# -- где бан на ноде не работает --

def _web_kinds(rep: dict) -> set[str]:
    return {str((w or {}).get("kind") or "") for w in rep.get("web_services") or [] if isinstance(w, dict)}


def notes(srv: Server, b: dict | None) -> list[dict]:
    """Честно: где бан на ноде не поможет. {k, level, text}; level 2 - бан не работает совсем,
    1 - работает не для всех сайтов."""
    rep = srv.last_report or {}
    out: list[dict] = []
    cf = ((srv.exposure or {}) if isinstance(srv.exposure, dict) else {}).get("cf") or {}
    n_cf, total = int(_num(cf.get("n"))), int(_num(cf.get("total")))
    if n_cf:
        whole = total and n_cf >= total
        out.append({"k": "cf", "level": 2 if whole else 1,
                    "text": (f"{'все сайты' if whole else f'{n_cf} из {total} сайтов'} за Cloudflare: посетители приходят "
                             "с адресов Cloudflare, бан на ноде их не режет - блокировать в Cloudflare (WAF)")})
    if b is None:
        return out
    cls = b.get("cls") if isinstance(b.get("cls"), dict) else {}
    pub, cfn, priv = (int(_num(cls.get(k))) for k in ("pub", "cf", "priv"))
    if cfn and not n_cf:
        out.append({"k": "cfedge", "level": 1,
                    "text": "в логах есть адреса Cloudflare: часть сайтов идет через него, для них бан на ноде не работает"})
    edge = sorted(_web_kinds(rep) & set(_EDGE_NO_LOG))
    if not int(_num(b.get("logs"))):
        out.append({"k": "nolog", "level": 2,
                    "text": (f"край {', '.join(edge)} без access-лога: адресов клиентов не видно, банить не по чему"
                             if edge else "access-логов веб-сервера не найдено: банить не по чему")})
    elif not pub and priv:
        out.append({"k": "priv", "level": 2,
                    "text": (f"в логах только внутренние адреса: перед nginx стоит {', '.join(edge) or 'прокси'} "
                             "без X-Forwarded-For или без своего access-лога, клиентов не видно")})
    if b.get("nft") is False:
        out.append({"k": "nft", "level": 2, "text": "на ноде нет nftables: банить нечем, работает только наблюдение"})
    return out


# -- что стоит алерта --

def _applied(b: dict, now: datetime) -> list[dict]:
    return [x for x in _list(b, "bans") if not x.get("w") and now.timestamp() - _num(x.get("since")) < 86400]


def muted_keys(srv: Server) -> set[str]:
    return {m[len(MUTE_PREFIX):] for m in (getattr(srv, "alert_mutes", None) or []) if m.startswith(MUTE_PREFIX)}


def problems(srv: Server, now: datetime) -> list[dict]:
    """Беды автобана: {key, level, text}. Уровень 2 у всех - каждая требует вмешательства."""
    b = block(srv.last_report, now)
    if b is None:
        return []
    out: list[dict] = []
    for x in _list(b, "self"):
        out.append({"key": f"self:{x['ip']}", "level": 2,
                    "text": f"автобан забанил свой адрес {x['ip']} (белый список: {x.get('label') or '?'}), бан снят"})
    mode = str(b.get("mode") or "")
    since = _num(b.get("err_since"))
    if mode == "on" and not b.get("can") and since and now.timestamp() - since >= CANT_HOLD:
        err = str(b.get("err") or "").strip()
        out.append({"key": "cant", "level": 2,
                    "text": "автобан включен, но не может банить" + (f": {err[:160]}" if err else "")})
    ap = _applied(b, now)
    leak = [x for x in ap if _num(x.get("seen")) > 0]
    if len(ap) >= NOEFFECT_MIN and len(leak) * 2 >= len(ap):
        out.append({"key": "noeffect", "level": 2,
                    "text": (f"бан не действует: {len(leak)} из {len(ap)} забаненных за сутки снова приходят - "
                             "трафик идет к ноде не напрямую (CDN, внешний балансер)")})
    auto = str(b.get("auto") or "")
    if auto.startswith("avalanche"):
        out.append({"key": "avalanche", "level": 2,
                    "text": "лавина банов: больше 100 за час, автобан сам перешел в наблюдение - проверьте, не ловушка ли это на живой трафик"})
    elif mode == "on" and int(_num(b.get("hour"))) >= AVALANCHE_HOUR:
        out.append({"key": "avalanche", "level": 2,
                    "text": f"лавина банов: {int(_num(b.get('hour')))} за час - проверьте, не ловушка ли это на живой трафик"})
    done = muted_keys(srv)
    return [p for p in out if p["key"] not in done and p["key"].split(":")[0] not in done]


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Автобан забанил свой адрес
# 203.0.113.5 (белый список: Мой VPN), бан снят

_SHORT = {"self": "Автобан забанил свой адрес", "cant": "Автобан включен, но не может банить",
          "noeffect": "Бан сканеров не действует", "avalanche": "Лавина банов сканеров"}


def tg_short(items) -> tuple[str, list[str]] | None:
    rows = [x for x in items or [] if isinstance(x, dict) and x.get("key")]
    if not rows:
        return None
    r = rows[0]
    kind = str(r["key"]).split(":")[0]
    what = _SHORT.get(kind, "Автобан сканеров не в порядке")
    text = str(r.get("text") or "")
    fact = text.split(": ", 1)[1] if ": " in text else text
    if kind == "self":
        fact = text.removeprefix("автобан забанил свой адрес ")
    facts = [fact]
    if len(rows) > 1:
        facts.append("еще: " + ", ".join(_SHORT.get(str(x["key"]).split(":")[0], "?").lower() for x in rows[1:3]))
    return what, facts


# -- карточка сервера --

def _ban_out(x: dict) -> dict:
    ip = str(x.get("ip"))
    return {"ip": ip, "cc": geoip.country_of(ip), "since": _num(x.get("since")), "until": _num(x.get("until")),
            "n": int(_num(x.get("n"), 1)), "hits": int(_num(x.get("hits"))), "paths": _paths(x),
            "more": int(_num(x.get("more"))), "where": str(x.get("where") or "")[:120],
            "watch": bool(x.get("w")), "seen": int(_num(x.get("seen"))), "why": why_text(x)}


def view(srv: Server, now: datetime) -> dict | None:
    """Блок "Автобан" в разделе Веб. None - helper не стоит или молчит."""
    b = block(srv.last_report, now)
    if b is None:
        return None
    bans = sorted((_ban_out(x) for x in _list(b, "bans")), key=lambda x: -x["since"])
    return {
        "mode": str(b.get("mode") or "watch"), "auto": str(b.get("auto") or ""), "can": bool(b.get("can")),
        "nft": b.get("nft") is not False, "err": str(b.get("err") or "")[:300], "logs": int(_num(b.get("logs"))),
        "hour": int(_num(b.get("hour"))), "day": int(_num(b.get("day"))), "drops": int(_num(b.get("drops"))),
        "bans": [x for x in bans if not x["watch"]], "would": [x for x in bans if x["watch"]],
        "own": [{"ip": str(x["ip"]), "cc": geoip.country_of(str(x["ip"])), "label": str(x.get("label") or ""),
                 "hits": int(_num(x.get("hits"))), "paths": _paths(x), "more": int(_num(x.get("more"))),
                 "last": _num(x.get("last"))} for x in _list(b, "own")],
        "proxy": [{"ip": str(x["ip"]), "n": int(_num(x.get("n"))), "share": int(_num(x.get("share")))}
                  for x in _list(b, "proxy")],
        "self": [{"ip": str(x["ip"]), "label": str(x.get("label") or ""), "ts": _num(x.get("ts"))}
                 for x in _list(b, "self")],
        "notes": notes(srv, b),
        "problems": problems(srv, now),
        "agent_ok": agent_ok(srv),
    }


# -- история: баны из отчета в таблицу --

_last_ts: dict[int, float] = {}


async def store(session, srv: Server, extras: dict | None, now: datetime) -> None:
    """Баны из свежего блока - в autoban_bans; пропавшие из блока - закрыты. Блок helper обновляет
    раз в минуту, а агент шлет его с каждым отчетом: повтор по ts блока отсекаем в памяти."""
    b = block({"extras": extras or {}}, now)
    if b is None:
        return
    ts = _num(b.get("ts"))
    if _last_ts.get(srv.id) == ts:
        return
    _last_ts[srv.id] = ts
    cur = {(x["ip"], int(_num(x.get("since")))): x for x in _list(b, "bans") if _num(x.get("since")) > 0}
    rows = list(await session.scalars(select(AutobanBan).where(
        AutobanBan.server_id == srv.id, AutobanBan.ended_at.is_(None))))
    have = {(r.ip, int(r.since.timestamp())): r for r in rows}
    for key, x in cur.items():
        r = have.get(key)
        if r is None:
            session.add(AutobanBan(
                server_id=srv.id, ip=str(x["ip"])[:64], since=_ts(x.get("since")), until=_ts(x.get("until")) or now,
                n=int(_num(x.get("n"), 1)), hits=int(_num(x.get("hits"))), paths=_paths(x), more=int(_num(x.get("more"))),
                where=str(x.get("where") or "")[:120], watch=bool(x.get("w")), seen=int(_num(x.get("seen"))),
                end="", digested=bool(x.get("w"))))
            continue
        r.seen = int(_num(x.get("seen")))
        r.until = _ts(x.get("until")) or r.until
    for key, r in have.items():
        if key in cur:
            continue
        until = r.until if r.until.tzinfo else r.until.replace(tzinfo=timezone.utc)
        r.ended_at = now
        r.end = "expired" if until <= now + timedelta(seconds=120) else (r.end or "gone")


async def history(session, server_id: int, days: int = 30, limit: int = 300,
                  now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = list(await session.scalars(
        select(AutobanBan).where(AutobanBan.server_id == server_id,
                                 AutobanBan.since >= now - timedelta(days=days))
        .order_by(AutobanBan.since.desc()).limit(limit)))
    return [{"ip": r.ip, "cc": geoip.country_of(r.ip), "since": r.since.timestamp(), "until": r.until.timestamp(),
             "n": r.n, "hits": r.hits, "paths": list(r.paths or []), "more": r.more, "where": r.where,
             "watch": r.watch, "seen": r.seen, "ended": r.ended_at.timestamp() if r.ended_at else None,
             "end": r.end or "", "why": why_text({"paths": r.paths, "more": r.more, "where": r.where, "hits": r.hits})}
            for r in rows]


async def mark_unban(session, server_id: int, ip: str) -> None:
    """Разбан из панели: бан закрывается как снятый человеком, а не истекший."""
    for r in await session.scalars(select(AutobanBan).where(
            AutobanBan.server_id == server_id, AutobanBan.ip == ip, AutobanBan.ended_at.is_(None))):
        r.end = "unban"


# -- настройки панели: белый список и сводка --

def norm_net(raw: str) -> str:
    """'203.0.113.5' -> '203.0.113.5/32', '2001:db8::/48' как есть; кривое - ValueError."""
    return str(ipaddress.ip_network(str(raw).strip(), strict=False))


async def load_settings(session) -> dict:
    raw = await settings_store.get_raw(session, SETTINGS_KEY)
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    allow = []
    for x in d.get("allow") or []:
        if not isinstance(x, dict):
            continue
        try:
            allow.append({"net": norm_net(x.get("net")), "label": str(x.get("label") or "")[:40]})
        except ValueError:
            continue
    digest = d.get("digest") if d.get("digest") in DIGEST_MODES else "day"
    return {"allow": allow, "digest": digest}


async def save_settings(session, cfg: dict) -> None:
    await settings_store.set_raw(session, SETTINGS_KEY, json.dumps(
        {"allow": cfg.get("allow") or [], "digest": cfg.get("digest") or "day"}, ensure_ascii=False))


# внутренние сети helper и так не банит (там же, что LOCAL_NETS в helper'е): в список их не кладем
_LOCAL = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                                            "127.0.0.0/8", "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128")]


def _public(ip: str) -> str | None:
    try:
        a = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if a.is_unspecified or any(a.version == n.version and a in n for n in _LOCAL):
        return None
    return str(a)


def panel_allow(cfg: dict, servers: list[Server], locations: list[Location]) -> list[tuple[str, str]]:
    """Белый список, который панель отдает нодам: свои записи пользователя, адреса всех серверов
    этой панели (они ходят друг к другу проверками связи) и выходы локаций проверок."""
    out: dict[str, str] = {}
    for x in cfg.get("allow") or []:
        out.setdefault(x["net"], x.get("label") or "панель")
    for s in servers:
        rep = s.last_report or {}
        for ip in {s.external_ip, s.agent_ip, s.local_ip, *(rep.get("ips") or [])}:
            p = _public(ip) if isinstance(ip, str) else None
            if p:
                out.setdefault(norm_net(p), s.name[:40])
    for loc in locations:
        p = _public(loc.exit_ip or "")
        if p:
            out.setdefault(norm_net(p), f"локация {loc.name}"[:40])
    return sorted(out.items())


def allow_sha(items: list[tuple[str, str]]) -> str:
    """Тот же отпечаток, что считает helper: sha256 отсортированных сетей по строке, 16 знаков."""
    if not items:
        return ""
    body = "".join(f"{n}\n" for n in sorted(n for n, _l in items))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


async def sync_allow(session_factory, now: datetime) -> int:
    """Стадия планировщика: белый список панели на ноды, где helper прислал другой отпечаток.
    Команда autoban mode=allow, не чаще раза в SYNC_EVERY на сервер. Возвращает, сколько поставлено."""
    queued = 0
    async with session_factory() as session:
        servers = list(await session.scalars(select(Server)))
        locations = list(await session.scalars(select(Location)))
        cfg = await load_settings(session)
        items = panel_allow(cfg, servers, locations)
        sha = allow_sha(items)
        paths = [f"{n} {lab}".strip() for n, lab in items]
        for s in servers:
            b = block(s.last_report, now)
            if not s.enabled or b is None or not agent_ok(s) or str(b.get("allow_sha") or "") == sha:
                continue
            last = await session.scalar(
                select(BackupCommand.created_at).where(
                    BackupCommand.server_id == s.id, BackupCommand.action == "autoban", BackupCommand.mode == "allow")
                .order_by(BackupCommand.id.desc()).limit(1))
            if last is not None:
                last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                if (now - last).total_seconds() < SYNC_EVERY:
                    continue
            session.add(BackupCommand(server_id=s.id, action="autoban", mode="allow", paths=paths, origin="auto"))
            queued += 1
        await session.commit()
    return queued


# -- сводка банов в Telegram --

def _fmt_until(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%d.%m %H:%M")


def digest_text(rows: list[tuple[Server, list[AutobanBan]]], period: str, base_url: str, tz) -> str:
    """Сводка банов одним сообщением (формат A, только символы клавиатуры):

    🚫 Автобан за сутки: 23 адреса на 3 серверах
    k8s-a - 14: 203.0.113.10 (NL) /.env, /.git/config +4 до 28.10 12:00; 198.51.100.4 ...
    Это сканеры, делать ничего не надо. Список и разбан - в карточке сервера, раздел Веб
    """
    total = sum(len(b) for _s, b in rows)
    word = "за сутки" if period == "day" else "за час"
    head = f"🚫 <b>{html.escape(tg_fmt.clean(f'Автобан {word}: {total} {_addr_word(total)} на {len(rows)} {_srv_word(len(rows))}'))}</b>"
    lines = [head]
    for s, bans in rows[:10]:
        url = f"{base_url}/?server={s.id}&sec=web" if base_url else ""
        parts = []
        for r in bans[:3]:
            cc = geoip.country_of(r.ip)
            p = ", ".join(list(r.paths or [])[:2]) + (f" +{r.more + max(len(r.paths or []) - 2, 0)}" if r.more or len(r.paths or []) > 2 else "")
            rep = " повтор" if r.n > 1 else ""
            parts.append(f"{r.ip}{f' ({cc})' if cc else ''} {p} до {_fmt_until(r.until.timestamp(), tz)}{rep}")
        tail = f"; и еще {len(bans) - 3}" if len(bans) > 3 else ""
        lines.append(f"{tg_fmt.link(s.name, url)} - {len(bans)}: {html.escape(tg_fmt.clean('; '.join(parts) + tail))}")
    if len(rows) > 10:
        lines.append(html.escape(f"и еще {len(rows) - 10} {_srv_word(len(rows) - 10)}"))
    lines.append("Это сканеры, делать ничего не надо. Список и разбан - в карточке сервера, раздел Веб")
    return "\n".join(lines)


def _addr_word(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "адресов"
    return {1: "адрес", 2: "адреса", 3: "адреса", 4: "адреса"}.get(n % 10, "адресов")


def _srv_word(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return "серверах"
    return "сервере" if n % 10 == 1 else "серверах"


def digest_due(mode: str, last: datetime | None, now: datetime, tz) -> bool:
    """Пора ли слать: раз в час - с прошлой сводки прошел час; раз в сутки - наступило DIGEST_HOUR
    по местному времени, а сегодня еще не слали."""
    if mode == "off":
        return False
    if mode == "hour":
        return last is None or (now - last).total_seconds() >= 3600
    loc = now.astimezone(tz)
    if loc.hour < DIGEST_HOUR:
        return False
    return last is None or last.astimezone(tz).date() < loc.date()


async def send_digest(session_factory, settings, now: datetime) -> bool:
    """Стадия планировщика: сводка новых банов (с файрволом, не наблюдения) одним сообщением на
    панель - раз в сутки в DIGEST_HOUR или раз в час, как выбрано в настройке. True - ушла."""
    async with session_factory() as s:
        mode = (await load_settings(s))["digest"]
        if mode == "off":
            return False
        try:
            st = json.loads(await settings_store.get_raw(s, DIGEST_KEY) or "{}")
        except ValueError:
            st = {}
        cfg = await settings_store.get_alert_config(s, settings)
        try:
            tz = ZoneInfo(cfg.get("tz") or "Europe/Moscow")
        except (KeyError, ValueError):
            tz = timezone.utc
        try:
            last = datetime.fromisoformat(st["last"]) if st.get("last") else None
        except (TypeError, ValueError):
            last = None
        if not digest_due(mode, last, now, tz):
            return False
        rows = list(await s.scalars(select(AutobanBan).where(
            AutobanBan.digested.is_(False), AutobanBan.watch.is_(False),
            AutobanBan.since >= now - timedelta(days=2)).order_by(AutobanBan.since)))
        servers = {x.id: x for x in await s.scalars(select(Server).where(Server.enabled.is_(True)))}
        muted = await settings_store.get_muted(s)
        by: dict[int, list[AutobanBan]] = {}
        for r in rows:
            if r.server_id in servers:
                by.setdefault(r.server_id, []).append(r)
        groups = sorted(((servers[k], v) for k, v in by.items()), key=lambda x: (-len(x[1]), x[0].name))
        text = digest_text(groups, mode, (settings.panel_url or "").rstrip("/"), tz) if groups else ""
        threshold = int(cfg.get("flood_threshold", 6))
        can = bool(groups) and not muted and alerts.alerts_enabled(cfg)
        ids = [r.id for r in rows]
    delivered = can and await alerts.dispatch(
        cfg, True, [alerts.Msg(text, "servers", "", kind="autoban_digest")], threshold,
        parse_mode="HTML", session_factory=session_factory)
    if delivered or not groups:
        async with session_factory() as s:
            for r in await s.scalars(select(AutobanBan).where(AutobanBan.id.in_(ids))):
                r.digested = True
            await settings_store.set_raw(s, DIGEST_KEY, json.dumps({"last": now.isoformat()}))
            await s.commit()
    return bool(delivered)
