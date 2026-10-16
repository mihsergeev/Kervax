"""Левые запросы к панели: кто долбит API отказами или шлет запросы слишком часто.

Повод: если с сервера в панель идет поток запросов, на которые панель отвечает отказом (агент
с токеном удаленного сервера, ansible со старым токеном, push удаленного монитора, сканер,
перебирающий адреса API) или просто слишком частых (несколько копий агента, агент в цикле
перезапусков), раньше это было видно только в логе nginx, и то если туда смотреть.

Как устроено:
  * middleware (RequestWatchMiddleware) в каждом процессе панели считает запросы к /api/ по
    адресу клиента (client_ip.client_key) и раз в минуту пишет в request_stats только минуты с
    отказами (BAD) или с всплеском (WRITE_FLOOR запросов в минуту и больше). Обычный агент шлет
    ~15 запросов в минуту, такие минуты не пишутся вовсе. Вход (/api/auth/login) не считаем:
    у неудачных входов своя защита и свой алерт (ratelimit.py), а /api/health дергает сам docker;
  * планировщик (check) раз в тик складывает последние WINDOW_MIN минут по адресам: BAD_LIMIT
    отказов или FLOOD_PER_SERVER запросов на каждый сервер за адресом (за NAT их бывает много) -
    алерт с тем, кто это (сервер панели или чужой адрес), что именно и на что похоже. Отбой -
    после QUIET_MIN минут тишины, чтобы поток с перерывами не дребезжал алертами. Состояние - в
    app_settings (STATE_KEY): по нему же "Что сломано" показывает это у сервера.
"""

import asyncio
import html
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import alerts, client_ip, settings_store
from app.models import RequestStat, Server

log = logging.getLogger(__name__)

BAD = frozenset({401, 403, 404, 405, 422, 429})
SKIP = frozenset({"/api/health", "/api/auth/login"})
WINDOW_MIN = 10
BAD_LIMIT = 50  # отказов за WINDOW_MIN с одного адреса
TYPICAL_PER_SERVER = 150  # столько запросов за 10 минут шлет обычный агент (опрос команд раз в 4 с)
FLOOD_PER_SERVER = 600  # в четыре раза больше обычного - всплеск
WRITE_FLOOR = 60  # минута без отказов пишется, только если запросов столько и больше
QUIET_MIN = 30
KEEP = timedelta(days=2)
STATE_KEY = "request_watch"
KIND = "bogus_requests"
_PATHS_KEPT = 5
_KEYS_MAX = 5000  # адресов за минуту в памяти: перебор адресов не съест память процесса

_buckets: dict[str, dict] = {}
_minute: int | None = None
_tasks: set[asyncio.Task] = set()
_TOKENISH = re.compile(r"[0-9a-fA-F-]{12,}|[A-Za-z0-9_-]{20,}")


def norm_path(path: str) -> str:
    """Адрес без id и токенов: /api/servers/12 -> /api/servers/{id}, /api/push/<токен> ->
    /api/push/{token}, чтобы одинаковые запросы складывались."""
    parts = []
    for seg in path.split("/")[:7]:
        if seg.isdigit():
            seg = "{id}"
        elif _TOKENISH.fullmatch(seg):
            seg = "{token}"
        parts.append(seg[:40])
    return "/".join(parts)[:120]


def count(key: str, path: str, status: int, minute: int | None = None) -> tuple[int, dict] | None:
    """Учесть запрос. Когда началась новая минута, возвращает снимок прошлой - его надо записать."""
    global _minute, _buckets
    m = int(time.time() // 60) if minute is None else minute
    snap = None
    if _minute is not None and m != _minute and _buckets:
        snap = (_minute, _buckets)
        _buckets = {}
    _minute = m
    b = _buckets.get(key)
    if b is None:
        if len(_buckets) >= _KEYS_MAX:
            return snap
        b = _buckets[key] = {"total": 0, "bad": 0, "codes": Counter(), "paths": Counter(), "top": Counter()}
    b["total"] += 1
    p = norm_path(path)
    if status in BAD:
        b["bad"] += 1
        b["codes"][status] += 1
        b["paths"][f"{status} {p}"] += 1
    else:
        b["top"][p] += 1
    return snap


def _rows(minute: int, buckets: dict) -> list[RequestStat]:
    ts = datetime.fromtimestamp(minute * 60, timezone.utc)
    out = []
    for key, b in buckets.items():
        if not b["bad"] and b["total"] < WRITE_FLOOR:
            continue
        paths = b["paths"] if b["bad"] else b["top"]
        out.append(RequestStat(
            minute=ts, key=key[:64], total=b["total"], bad=b["bad"],
            codes={str(c): n for c, n in b["codes"].items()}, paths=dict(paths.most_common(_PATHS_KEPT)),
        ))
    return out


async def write(session_factory, snap: tuple[int, dict]) -> None:
    rows = _rows(*snap)
    if not rows:
        return
    try:
        async with session_factory() as s:
            s.add_all(rows)
            await s.commit()
    except Exception:  # noqa: BLE001 - счетчик не должен ронять запросы
        log.warning("счетчик запросов к панели не записан", exc_info=True)


async def flush(session_factory) -> None:
    """Записать текущую минуту сразу (тесты)."""
    global _buckets, _minute
    if _minute is not None and _buckets:
        snap = (_minute, _buckets)
        _buckets = {}
        await write(session_factory, snap)


def reset() -> None:
    global _buckets, _minute
    _buckets, _minute = {}, None


class RequestWatchMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/") or path in SKIP:
            await self.app(scope, receive, send)
            return
        code = 0

        async def _send(msg) -> None:
            nonlocal code
            if msg["type"] == "http.response.start":
                code = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                xff = next((v.decode("latin-1") for k, v in scope.get("headers") or [] if k == b"x-forwarded-for"), "")
                host = (scope.get("client") or (None,))[0]
                snap = count(client_ip.client_key(host, xff), path, code or 500)
                factory = getattr(getattr(scope.get("app"), "state", None), "session_factory", None)
                if snap and factory is not None:
                    t = asyncio.get_running_loop().create_task(write(factory, snap))
                    _tasks.add(t)
                    t.add_done_callback(_tasks.discard)
            except Exception:  # noqa: BLE001
                log.debug("счетчик запросов к панели", exc_info=True)


# --- разбор: кто это и на что похоже -------------------------------------------------------

def servers_by_ip(servers: list[Server]) -> dict[str, list[Server]]:
    out: dict[str, list[Server]] = {}
    for s in servers:
        rep = s.last_report or {}
        ips = {s.external_ip, s.agent_ip, s.local_ip, *(rep.get("ips") or [])}
        for ip in ips:
            if ip and isinstance(ip, str):
                out.setdefault(ip, []).append(s)
    return out


def aggregate(rows) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        a = out.setdefault(r.key, {"total": 0, "bad": 0, "codes": Counter(), "paths": Counter(),
                                   "first": r.minute, "last": r.minute, "peak": 0})
        a["total"] += r.total or 0
        a["bad"] += r.bad or 0
        a["codes"].update({int(c): int(n) for c, n in (r.codes or {}).items()})
        a["paths"].update({str(p): int(n) for p, n in (r.paths or {}).items()})
        a["first"], a["last"] = min(a["first"], r.minute), max(a["last"], r.minute)
        a["peak"] = max(a["peak"], r.total or 0)
    return out


def hint(a: dict, n_srv: int) -> str:
    """На что это похоже - по самому частому отказу или адресу всплеска."""
    if a["bad"] >= BAD_LIMIT:
        bad = Counter({p: n for p, n in a["paths"].items() if p[:3].isdigit()})
        top = bad.most_common(1)[0][0] if bad else ""
        code, _, path = top.partition(" ")
        distinct = len({p.partition(" ")[2] for p in bad})
        if code in ("401", "403") and path.startswith("/api/agent/"):
            return ("агент шлет неверный токен: сервер переустановили или удалили из панели, а агент на нем "
                    "остался. Переустановите агент или удалите его с сервера")
        if code == "401" and path.startswith("/api/ansible/"):
            return "ansible ходит со старым токеном: выпустите новый в окне Ansible и обновите инвентарь"
        if code == "404" and path.startswith("/api/push/"):
            return "push-монитор удален или его адрес сменился, а скрипт продолжает слать"
        if code == "404" and distinct >= 5:
            return "перебирает адреса API, похоже на сканер"
        if code == "429":
            return "запросы упираются в лимит панели"
        if code in ("401", "403"):
            return "запросы без входа или с чужим или просроченным токеном"
        if code == "422":
            return "запросы с неправильными данными: похоже на старую версию агента или скрипта"
        return ""
    top = a["paths"].most_common(1)[0][0] if a["paths"] else ""
    if top.startswith("/api/agent/"):
        return ("агент шлет запросы слишком часто: похоже, на сервере работает несколько его копий или он "
                "перезапускается в цикле")
    return f"чаще всего {top}" if top else ""


def what(a: dict, n_srv: int) -> str:
    if a["bad"] >= BAD_LIMIT:
        codes = ", ".join(f"{c} - {n}" for c, n in sorted(a["codes"].items(), key=lambda x: -x[1]))
        bad = Counter({p: n for p, n in a["paths"].items() if p[:3].isdigit()})
        top = bad.most_common(1)[0][0].partition(" ")[2] if bad else ""
        return (f"{a['bad']} отказов за {WINDOW_MIN} минут ({codes})"
                + (f", чаще всего {top}" if top else ""))
    usual = TYPICAL_PER_SERVER * max(1, n_srv)
    return f"{a['total']} запросов за {WINDOW_MIN} минут, обычно около {usual}"


def who(key: str, srvs: list[Server]) -> tuple[str, str]:
    """Текст "с кого" для алерта и короткое имя для истории."""
    if not srvs:
        return f"с адреса {key} (не сервер панели)", key
    names = sorted(s.name or f"#{s.id}" for s in srvs)
    if len(names) == 1:
        return f"с сервера {names[0]} ({key})", names[0]
    shown = ", ".join(names[:3]) + (f" и еще {len(names) - 3}" if len(names) > 3 else "")
    return f"с адреса {key} (за ним серверы {shown})", key


def _over(a: dict, n_srv: int) -> bool:
    return a["bad"] >= BAD_LIMIT or a["total"] >= FLOOD_PER_SERVER * max(1, n_srv)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _silenced(srvs: list[Server], now: datetime) -> bool:
    """Все серверы за адресом приглушили этот алерт или заглушены целиком."""
    def quiet(s: Server) -> bool:
        if s.snooze_until is not None and _aware(s.snooze_until) > now:
            return True
        return KIND in set(s.alert_mutes or [])
    return bool(srvs) and all(quiet(s) for s in srvs)


async def load_state(session) -> dict:
    raw = await settings_store.get_raw(session, STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def check(session_factory, settings, now: datetime) -> None:
    """Стадия планировщика: алерт на адреса с потоком отказов или всплеском и отбой."""
    async with session_factory() as s:
        rows = list(await s.scalars(select(RequestStat).where(
            RequestStat.minute >= now - timedelta(minutes=WINDOW_MIN))))
        state = await load_state(s)
        if not rows and not state:
            return
        servers = list(await s.scalars(select(Server)))
        cfg = await settings_store.get_alert_config(s, settings)
        muted = await settings_store.get_muted(s)
        rule = (await settings_store.get_server_alert_rules(s)).get(KIND) or {}
    by_ip = servers_by_ip(servers)
    agg = aggregate(rows)
    new: dict[str, dict] = {}
    fires: list[tuple[str, str, str]] = []  # (адрес, текст, кому в историю)
    recs: list[tuple[str, str, str]] = []
    default = settings_store.SERVER_ALERT_KINDS[KIND][1]
    tpl = rule.get("text") or default
    for key, a in agg.items():
        srvs = by_ip.get(key, [])
        if not _over(a, len(srvs)):
            continue
        cur = state.get(key) or {}
        w_txt, h_txt = what(a, len(srvs)), hint(a, len(srvs))
        item = {"since": cur.get("since") or now.isoformat(), "last_over": now.isoformat(),
                "notified": bool(cur.get("notified")), "servers": [x.id for x in srvs],
                "what": w_txt, "hint": h_txt}
        new[key] = item
        if item["notified"] or _silenced(srvs, now):
            continue
        w_who, target = who(key, srvs)
        fields = {"who": html.escape(w_who), "what": html.escape(w_txt), "hint": html.escape(h_txt)}
        try:
            body = tpl.format(**fields)
        except (KeyError, IndexError, ValueError):
            body = default.format(**fields)
        if not h_txt:
            body = body.replace("\n↳ ", "").rstrip()
        fires.append((key, f"🛑 {body}", target))
    for key, cur in state.items():
        if key in new:
            continue
        last = cur.get("last_over")
        try:
            held = last and now - datetime.fromisoformat(last) < timedelta(minutes=QUIET_MIN)
        except ValueError:
            held = False
        if held:
            new[key] = cur  # затих недавно: держим, чтобы поток с перерывами не дребезжал
            continue
        if cur.get("notified"):
            srvs = [x for x in servers if x.id in set(cur.get("servers") or [])]
            w_who, target = who(key, srvs)
            recs.append((key, f"✅ Левые запросы {html.escape(w_who)} прекратились", target))
    can_send = bool(rule.get("enabled", True)) and not muted and alerts.alerts_enabled(cfg)
    msgs = [alerts.Msg(t, "servers", "", kind=KIND, target=tg, recovery=False) for _k, t, tg in fires]
    msgs += [alerts.Msg(t, "servers", "", kind=KIND, target=tg, recovery=True) for _k, t, tg in recs]
    delivered = bool(msgs) and can_send and await alerts.dispatch(
        cfg, True, msgs, int(cfg.get("flood_threshold", 6)), parse_mode="HTML",
        session_factory=session_factory,
    )
    if delivered:
        for key, _t, _tg in fires:
            new[key]["notified"] = True
    else:
        for key, _t, _tg in recs:  # отбой не дошел - попробуем на следующем тике
            new[key] = state[key]
    if new != state:
        async with session_factory() as s:
            await settings_store.set_raw(s, STATE_KEY, json.dumps(new, ensure_ascii=False))


async def server_issues(session) -> dict[int, dict]:
    """Проблема "шлет в панель левые запросы" для "Что сломано" и списка серверов: по серверам,
    чей адрес сейчас в состоянии алерта."""
    out: dict[int, dict] = {}
    for key, cur in (await load_state(session)).items():
        ids = cur.get("servers") or []
        if not ids:
            continue
        # коротко: на что похоже (до двоеточия подсказки) и сколько, без самых частых адресов
        head = (cur.get("hint") or "").split(":")[0]
        short = ", ".join(x for x in (head, (cur.get("what") or "").split(" (")[0]) if x)
        text = f"шлет в панель левые запросы с {key}: {short}"
        if len(ids) > 1:
            text = f"за адресом {key} кто-то шлет в панель левые запросы: {short}"
        for sid in ids:
            out[sid] = {"kind": KIND, "level": 2, "text": text, "sec": "", "mute": KIND,
                        "since": cur.get("since"), "hint": cur.get("hint", "")}
    return out
