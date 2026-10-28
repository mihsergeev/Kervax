"""Жизнь заглушек алертов сервера: есть ли сейчас то, что заглушка глушит, и с каких пор тихо.

Человек глушит то, что спамит, и забывает: проблема давно ушла, а заглушка висит, и следующая такая же
беда придет молча. Сборщик каждый тик смотрит на каждую заглушку алертов сервера (мьют насовсем, снуз
до даты, снуз всего сервера), есть ли сейчас то, что она глушит, и пишет это в alert_state["mute_live"]:
{ключ: {"on": 1|0, "t": с какого момента так, "w": что именно сейчас есть, "seen" и "u": когда панель
впервые увидела заглушку с этим сроком и сам срок}}. Интерфейс пишет рядом с заглушкой "тихо 2 дн" или
"сейчас есть: ...". Чего панель не видит (событие без состояния, нода молчит), про то молчит и она.

Временная заглушка, под которой тихо сутки (QUIET_LIFT), снимается сама. В Telegram ничего: ушедшее под
заглушкой и так отбивается обычной дорогой (сборщик, ветка _muted), а тут проблемы нет вовсе. В журнал -
mute_lifted.

Временная заглушка от суток (HOLD_MIN) кончается, а проблема есть: алерты не летят пачкой в минуту
окончания. За LEAD до конца сборщик продлевает снуз шагами HOLD_STEP и помечает его в
alert_state["mute_over"]: {ключ: {"at": исходный срок, "ext": до чего продлено, "lvl", "sig"}}. На главной
строка "заглушка кончилась, а проблема есть" с кнопками продлить, насовсем, вернуть. Держится, пока не
стало хуже (уровень выше или новое в sig - снимается, и алерт идет как обычно), и снимается молча, когда
проблемы нет GONE. Человек поменял срок или снял снуз - это его решение, пометка уходит. Короткие снузы
("1 ч" и "до утра" из Telegram) - это "напомни позже", по ним как раньше.

Насовсем - осознанное решение: сама не снимается и не напоминает, только "тихо 40 дн" рядом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Server

LIVE = "mute_live"
OVER = "mute_over"
# снуз всего сервера (snooze_until), ключ как у фронта (mutes.tsx, key '')
SERVER = ""

QUIET_LIFT = 86400
HOLD_MIN = 86400 - 600
LEAD = 600
HOLD_STEP = 86400
HOLD_RENEW = 3600
GONE = 600

# Решения "так и должно быть" и заглушки до нового (mutes.tsx DECISIONS, home:, у поломки диска своя
# жизнь в disk_mute): это не заглушенный алерт, про них "тихо" ни о чем не говорит
DECISIONS = ("access:", "exposed:", "cfip:", "memlim:", "dockrisk:", "noboot:", "osupd:", "swver:", "ssh:",
             "proxyold:", "bkhttp:", "home:")

# Что выдает collector.server_problems ("Что сломано"): про эти виды "тихо" значит "нет в сводке".
# Тест сверяет с исходником
PROBLEM_KINDS = frozenset({
    "access_change", "access_risk", "autoban", "backup_check", "backup_cron", "backup_dump", "backup_dump_space",
    "backup_mirror", "backup_restore", "cf_realip", "clock", "conntrack", "cpu", "cpu_spin", "cron_stuck", "db_conn", "db_health",
    "disk", "disk_forecast", "disk_health", "disk_slow", "disktemp", "dns", "docker_restarts", "exposed",
    "flux_down", "flux_stale", "fpm", "gitlab", "gpu", "gpu_heat", "inode", "kube_cron", "kube_expiry",
    "kube_job", "kube_node", "kube_pod", "kube_restarts", "link", "listen_gone", "llm", "log_errors", "mem",
    "mem_leak", "mem_limit", "net_health", "oom", "port_new", "pressure", "queue", "queue_failed", "queue_stuck",
    "ssh_risk", "temp", "throttle", "traffic", "unit_loop", "units", "web_5xx", "web_cert", "web_config",
    "webfiles", "workers",
})
# Поштучные ключи, которые "Что сломано" отдает своим mute: без поштучных заглушек (shadow) вещь видна там
# под своим ключом, нет ее там - тихо
SHADOW_PREFIXES = ("unit:", "qstuck:", "qfail:", "worker:", "cron:", "logerr:", "fpm:", "autoban:", "gitlab:",
                   "listen:", "leak:", "restarts:", "oom:")
# Поштучные ключи, которые сборщик проверяет сам (collector.mute_present): поды, Job, CronJob, контейнеры
PRESENT_PREFIXES = ("kube_pod:", "kube_job:", "kube_cron:", "docker_down:", "docker_loop:")


def _ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def watched(k: str) -> bool:
    return bool(k) and not k.startswith(DECISIONS)


def item(k: str) -> bool:
    """Ключ про одну вещь (unit:nginx.service), а не про вид целиком (units, disk@1)."""
    return ":" in k


def keys(s: Server, now: datetime) -> dict[str, str | None]:
    """Действующие заглушки алертов сервера: {ключ: срок ISO | None - насовсем}. Насовсем главнее снуза."""
    out: dict[str, str | None] = {}
    su = _ts(s.snooze_until)
    if su is not None and su > now:
        out[SERVER] = su.isoformat()
    for k, u in (s.alert_snoozes or {}).items():
        t = _ts(u)
        if watched(str(k)) and t is not None and t > now:
            out[str(k)] = t.isoformat()
    for k in s.alert_mutes or []:
        if watched(str(k)):
            out[str(k)] = None
    return out


def shadow(s: Server) -> object:
    """Сервер без поштучных заглушек: что было бы в "Что сломано", если их снять. Подменяет только
    alert_mutes и alert_snoozes, остальное берет у самого сервера."""
    return _Bare(s, [m for m in s.alert_mutes or [] if not item(str(m)) or str(m).startswith(DECISIONS)],
                 {k: v for k, v in (s.alert_snoozes or {}).items() if not item(str(k))})


class _Bare:
    def __init__(self, s: Server, mutes: list, snoozes: dict) -> None:
        self._s = s
        self.alert_mutes = mutes
        self.alert_snoozes = snoozes

    def __getattr__(self, name: str):
        return getattr(self._s, name)


def _lvl(c: tuple[int, dict]) -> int:
    lvl, ctx = c
    return max(int(lvl or 0), int(ctx.get("raw") or 0) if isinstance(ctx, dict) else 0)


def _texts(rows: list[str]) -> str:
    rows = [r for r in rows if r]
    out = "; ".join(rows[:2]) + (f" и еще {len(rows) - 2}" if len(rows) > 2 else "")
    return out[:300]


def judge(key: str, conds: dict[str, tuple[int, dict]], probs: list[dict], shadow_probs: list[dict] | None,
          present: dict[str, dict[str, str]], muted_items: set[str]) -> tuple[bool, str] | None:
    """Есть ли сейчас то, что глушит ключ: (есть, что именно) или None - панель этого не видит.
    probs - "Что сломано" сервера, shadow_probs - то же без поштучных заглушек (None - их нет),
    present - вещи, которые сборщик проверяет сам ({"kube_pod:": {ns/ворклоад: текст}}), muted_items -
    поштучные ключи, заглушенные сейчас (вид целиком судим без них: про них молчит не он)."""
    if key == SERVER:
        kinds = {k for k, c in conds.items() if _lvl(c) > 0} | {str(p.get("kind")) for p in probs}
        return bool(kinds), _texts([str(p.get("text") or "") for p in probs])
    if item(key):
        pre = key.split(":", 1)[0] + ":"
        if pre in PRESENT_PREFIXES:
            got = present.get(pre)
            if got is None:
                return None
            name = key[len(pre):]
            return (name in got), got.get(name, "")
        if pre in SHADOW_PREFIXES and shadow_probs is not None:
            hit = [str(p.get("text") or "") for p in shadow_probs if p.get("mute") == key]
            return bool(hit), _texts(hit)
        return None
    kind = key.split("@", 1)[0]
    if kind in ("docker_down", "docker_loop"):
        got = present.get(kind + ":")
        if got is None:
            return None
        names = [n for n in got if f"{kind}:{n}" not in muted_items]
        return bool(names), _texts([got[n] for n in names])
    rows = [str(p.get("text") or "") for p in probs if p.get("kind") == kind]
    c = conds.get(kind)
    if c is None and kind not in PROBLEM_KINDS:
        return None
    on = bool(rows) or (c is not None and _lvl(c) > 0)
    detail = "" if rows or c is None else str((c[1] or {}).get("detail") or "")
    return on, _texts(rows) or detail[:300]


def live_step(prev: dict | None, cur: dict[str, str | None], judged: dict[str, tuple[bool, str] | None],
              now: datetime) -> dict:
    """Новое alert_state["mute_live"]: on/t меняются со сменой "есть - нет", seen/u - со сменой срока.
    Заглушка, о которой судить нечего, держит прежнее (нода молчит - не значит, что тихо).
    prev None - панель здесь еще не судила (первый тик после выкатки): у заглушек, которые она застала
    уже стоящими, начала не знаем, seen нет."""
    first = not isinstance(prev, dict)
    prev = {} if first else prev
    out: dict[str, dict] = {}
    for k, until in cur.items():
        old = prev.get(k) if isinstance(prev.get(k), dict) else None
        e = dict(old or {})
        if old is None or e.get("u") != until:
            e["seen"] = None if first else now.isoformat()
            e["u"] = until
        j = judged.get(k)
        if j is not None:
            on = 1 if j[0] else 0
            if e.get("on") != on or not e.get("t"):
                e["on"], e["t"] = on, now.isoformat()
            if on and j[1]:
                e["w"] = j[1]
            else:
                e.pop("w", None)
        out[k] = e
    return out


def view(s: Server) -> tuple[dict[str, dict], dict[str, dict]]:
    """Для ServerOut: (mute_live без служебного seen/u, mute_over - только исходный срок). Удержание, у
    которого срок уже поменяли руками ("на день" с главной), не отдается сразу, не дожидаясь тика."""
    st = s.alert_state or {}
    stored = {str(k): v for k, v in (s.alert_snoozes or {}).items()}
    stored[SERVER] = s.snooze_until
    live = st.get(LIVE) if isinstance(st.get(LIVE), dict) else {}
    over = st.get(OVER) if isinstance(st.get(OVER), dict) else {}
    return ({k: {f: e[f] for f in ("on", "t", "w") if f in e} for k, e in live.items()
             if isinstance(e, dict) and "on" in e},
            {k: {"at": o.get("at")} for k, o in over.items()
             if isinstance(o, dict) and _ts(o.get("ext")) is not None and _ts(stored.get(k)) == _ts(o.get("ext"))})


def _sig(c: tuple[int, dict] | None) -> list[str] | None:
    if c is None or not isinstance(c[1], dict) or "sig" not in c[1]:
        return None
    return sorted(map(str, c[1].get("sig") or []))


def snapshot(key: str, conds: dict[str, tuple[int, dict]]) -> dict:
    """Что было, когда удержание началось: по нему "стало хуже"."""
    if key == SERVER:
        return {"kinds": {k: _lvl(c) for k, c in conds.items() if _lvl(c) > 0}}
    if item(key):
        return {}
    c = conds.get(key.split("@", 1)[0])
    return {"lvl": _lvl(c) if c else 0, "sig": _sig(c)}


def worse(key: str, snap: dict, conds: dict[str, tuple[int, dict]]) -> str:
    """Стало хуже, чем при начале удержания: что именно ("" - не стало)."""
    if key == SERVER:
        was = snap.get("kinds") or {}
        up = [k for k, c in conds.items() if _lvl(c) > int(was.get(k) or 0)]
        return f"новое: {', '.join(sorted(up))}" if up else ""
    if item(key):
        return ""
    c = conds.get(key.split("@", 1)[0])
    if c is None:
        return ""
    if _lvl(c) > int(snap.get("lvl") or 0):
        return "уровень выше"
    sig, was = _sig(c), snap.get("sig")
    if sig and was is not None and not set(sig) <= set(was):
        return "новое: " + ", ".join(sorted(set(sig) - set(was))[:3])
    return ""


def plan(cur: dict[str, str | None], live: dict, over: dict | None, conds: dict[str, tuple[int, dict]],
         stored: dict[str, str | None], now: datetime, lift: bool = True
         ) -> tuple[dict, list[tuple[str, str, str | None, str]]]:
    """Решения по временным заглушкам за тик: (новое alert_state["mute_over"], [(ключ, срок, который
    должен стоять сейчас, новый срок | None - снять, запись в журнал | "")]).
    cur - keys() (действующие), stored - сроки как лежат в базе (alert_snoozes и snooze_until, в том числе
    истекшие): сверка с ними - чтобы не затереть то, что человек поставил между тиками. lift False - тихие
    не снимать (нода молчит, и "тихо" - это то, что было до нее)."""
    over = dict(over) if isinstance(over, dict) else {}
    acts: list[tuple[str, str, str | None, str]] = []
    # удержание: человек поменял срок или снял - его решение; проблема ушла - снять молча; хуже - снять
    for k, o in list(over.items()):
        ext = o.get("ext") if isinstance(o, dict) else None
        if not ext or _ts(stored.get(k)) != _ts(ext):
            over.pop(k)
            continue
        e = live.get(k) or {}
        t = _ts(e.get("t"))
        if e.get("on") == 0 and t is not None and (now - t).total_seconds() >= GONE:
            over.pop(k)
            acts.append((k, ext, None, "заглушка снята: проблема ушла"))
            continue
        why = worse(k, o, conds)
        if why:
            over.pop(k)
            acts.append((k, ext, None, f"заглушка снята: стало хуже ({why})"))
            continue
        e_t = _ts(ext)
        if e_t is not None and (e_t - now).total_seconds() < HOLD_RENEW:
            new = (now + timedelta(seconds=HOLD_STEP)).isoformat()
            o = {**o, "ext": new}
            over[k] = o
            acts.append((k, ext, new, ""))
    for k, until in cur.items():
        if until is None or k in over:
            continue
        e = live.get(k) or {}
        u = _ts(until)
        if u is None:
            continue
        if e.get("on") == 1:
            seen = _ts(e.get("seen"))
            long_ = seen is None or (u - seen).total_seconds() >= HOLD_MIN
            if long_ and (u - now).total_seconds() <= LEAD:
                ext = (u + timedelta(seconds=HOLD_STEP)).isoformat()
                over[k] = {"at": until, "ext": ext, **snapshot(k, conds)}
                acts.append((k, until, ext, ""))
            continue
        t = _ts(e.get("t"))
        if lift and e.get("on") == 0 and t is not None and (now - t).total_seconds() >= QUIET_LIFT:
            acts.append((k, until, None, "заглушка снята: проблемы нет уже сутки"))
    return over, acts
