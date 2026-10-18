"""PHP-FPM: пул упирается в pm.max_children, запросы ждут в очереди или убиваются по таймауту.

У пула жесткий предел воркеров (pm.max_children, в стоковом docker-образе 5). Когда все заняты,
новые запросы ждут в очереди сокета, nginx отвечает медленно, потом 502/504, а в списке процессов
ничего необычного: те же пять php-fpm, что всегда. FPM пишет об этом в свой лог ("server reached
pm.max_children setting"), куда никто не смотрит. Helper fpm-setup раз в минуту находит мастера
php-fpm на ноде (контейнеры, поды, хост), читает их конфиг (пулы, listen, pm, pm.max_children,
таймауты slowlog и terminate), трижды за прогон считает соединения пула по /proc/PID/net его
сети: установленные на порт или сокет пула - обслуживаемые плюс ждущие, очередь слушающего
TCP-сокета - ждущие. Лог FPM читается по смещению: "reached pm.max_children", "executing too
slow", "execution timed out", по пулам, в 10-минутных корзинах за сутки. extras "fpm" -
{"v": 1, "ts", "pools": [{"where", "pool", "listen", "pm", "max", "children", "conns", "queue",
"sat", "span", "reached", "reached_d", "slow", "slow_d", "tout", "tout_d", "slowlog",
"terminate", "status", "slow_last", "slow_ts", "tout_last", "tout_ts"}], "skipped"}. sat -
минут за последний час, когда запросы ждали свободного воркера; reached/slow/tout - за час,
*_d - за сутки.

Беда - когда пул не справляется подолгу, а не всплеск на минуту: запросы ждали не меньше SAT_MIN
минут из часа, или пул упирался в предел не меньше REACHED_MIN раз за час, или по таймауту убито
не меньше TOUT_MIN запросов за час. Проблема - от SAT_HOT минут или TOUT_HOT убитых. Медленные
запросы только показываются. После ушедшего алерта отбой при половине порогов.

Заглушить пул можно насовсем (alert_mutes "fpm:<ключ>") или на время (alert_snoozes с тем же
ключом); ключ - "<где>|<пул>", например "контейнер shop-php|www".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Server
from app.queue_watch import where_text

MUTE_PREFIX = "fpm:"
SAT_MIN = 15  # минут из часа запросы ждали воркера
SAT_HOT = 45
REACHED_MIN = 60  # раз за час пул уперся в pm.max_children
TOUT_MIN = 20  # запросов за час убито по request_terminate_timeout
TOUT_HOT = 100
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("fpm")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def label(p: dict) -> str:
    """Где пул словами: "контейнер shop-php, пул www" """
    return f"{where_text(str(p.get('where') or ''))}, пул {p.get('pool') or ''}"


def key(p: dict) -> str:
    return f"{where_text(str(p.get('where') or ''))}|{p.get('pool') or ''}"


def _i(p: dict, k: str) -> int:
    try:
        return int(p.get(k) or 0)
    except (TypeError, ValueError):
        return 0


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def level(p: dict, held: bool = False) -> int:
    """0 - справляется, 1 - предупреждение, 2 - проблема. held - алерт уже ушел, отбой при
    половине порогов"""
    k = 0.5 if held else 1
    sat, tout = _i(p, "sat"), _i(p, "tout")
    if sat >= SAT_HOT or tout >= TOUT_HOT:
        return 2
    if sat >= SAT_MIN * k or _i(p, "reached") >= REACHED_MIN * k or tout >= TOUT_MIN * k:
        return 1
    return 0


def why(p: dict, held: bool = False) -> str:
    k = 0.5 if held else 1
    mx, parts = _i(p, "max"), []
    if _i(p, "sat") >= SAT_MIN * k:
        parts.append(f"запросы ждали свободного воркера {_i(p, 'sat')} мин из последнего часа"
                     + (f" (воркеров {mx})" if mx else ""))
    n = _i(p, "reached")
    if n >= REACHED_MIN * k:
        parts.append(f"за час пул {n} {_plural(n, 'раз', 'раза', 'раз')} уперся в pm.max_children = {mx}")
    n, tt = _i(p, "tout"), _i(p, "terminate")
    if n >= TOUT_MIN * k:
        parts.append(f"запросов, убитых по таймауту{f' {tt} с' if tt else ''}, за час: {n}")
    return ", ".join(parts)


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


def _held(s: Server) -> set[str]:
    return {str(k) for k in ((s.alert_state or {}).get("fpm_sig") or [])}


def pools(rep: dict) -> list[dict]:
    return [x for x in (block(rep) or {}).get("pools") or [] if isinstance(x, dict)]


def problems(s: Server, now: datetime) -> list[dict]:
    """Пулы, которые не справляются, без заглушенных: {"key", "label", "level", "why", "text"}"""
    held, muted = _held(s), _muted(s, now)
    out = []
    for x in pools(s.last_report or {}):
        k = key(x)
        h = k in held
        lvl = level(x, h)
        if not lvl or k in muted:
            continue
        w = why(x, h)
        text = f"PHP-FPM {label(x)}: {w}"
        ex = str(x.get("tout_last") or x.get("slow_last") or "").strip()
        if ex:
            text += f"\n↳ {ex[:200]}"
        out.append({"key": k, "label": label(x), "level": lvl, "why": w, "text": text})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


_INT = ("max", "children", "conns", "queue", "sat", "span", "reached", "reached_d", "slow", "slow_d", "tout",
        "tout_d", "slowlog", "terminate", "status", "slow_ts", "tout_ts")


def view(s: Server, now: datetime) -> dict | None:
    """Для раздела "PHP-FPM": {"ts", "pools", "skipped"}; у пула "key", "label", конфиг, соединения
    и очередь сейчас, счет за час и сутки, беда ("level", "why") и мьют"""
    b = block(s.last_report or {})
    if b is None:
        return None
    held, muted = _held(s), _muted(s, now)
    perm = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    snz = {str(k)[len(MUTE_PREFIX):]: str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()
           if str(k).startswith(MUTE_PREFIX)}
    rows = []
    for x in pools(s.last_report or {}):
        k = key(x)
        row = {"key": k, "label": label(x), "pool": str(x.get("pool") or ""), "listen": str(x.get("listen") or ""),
               "pm": str(x.get("pm") or ""), "slow_last": str(x.get("slow_last") or "")[:300],
               "tout_last": str(x.get("tout_last") or "")[:300]}
        row.update({f: _i(x, f) for f in _INT})
        lvl = level(x, k in held)
        if lvl:
            row["level"], row["why"] = lvl, why(x, k in held)
        if k in perm:
            row["mute"] = {"perm": True}
        elif k in muted and k in snz:
            row["mute"] = {"until": snz[k]}
        rows.append(row)
    sk = [{"where": where_text(str(x.get("where") or "")), "why": str(x.get("why") or "")}
          for x in b.get("skipped") or [] if isinstance(x, dict)]
    return {"ts": int(b.get("ts") or 0), "pools": rows, "skipped": sk}
