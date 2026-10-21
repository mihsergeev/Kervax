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
"terminate", "status", "slow_last", "slow_ts", "tout_last", "tout_ts", "crash", "crash_d",
"crash_last", "crash_ts"}], "skipped", "kern": {"n", "n_d", "last", "ts"}}. sat - минут за последний
час, когда запросы ждали свободного воркера; reached/slow/tout/crash - за час, *_d - за сутки.

Падения (helper 0.2): воркер, убитый сигналом сбоя ("child N exited on signal 11 (SIGSEGV)", еще 4,
6, 7, 8) - по логу FPM у пула; php-процессы, убитые segfault или general protection, - по журналу
ядра ноды (kern), он видит и поды, у которых лог FPM не нашелся. Воркеры, падающие подряд, - так
выглядят попытки взлома через ошибки памяти PHP (php2shell, октябрь 2026) или битое расширение.

Беда - когда пул не справляется подолгу, а не всплеск на минуту: запросы ждали не меньше SAT_MIN
минут из часа, или пул упирался в предел не меньше REACHED_MIN раз за час, или по таймауту убито
не меньше TOUT_MIN запросов за час, или воркеры упали не меньше CRASH_MIN раз за час. Проблема - от
SAT_HOT минут, TOUT_HOT убитых или CRASH_HOT падений. Медленные запросы только показываются. После
ушедшего алерта отбой при половине порогов. Падения по журналу ядра - отдельной строкой "ядро|php"
с теми же порогами, если их не объясняет уже упавший пул.

Заглушить пул можно насовсем (alert_mutes "fpm:<ключ>") или на время (alert_snoozes с тем же
ключом); ключ - "<где>|<пул>", например "контейнер shop-php|www".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app import tg_fmt
from app.models import Server
from app.queue_watch import where_in, where_text

MUTE_PREFIX = "fpm:"
SAT_MIN = 15  # минут из часа запросы ждали воркера
SAT_HOT = 45
REACHED_MIN = 60  # раз за час пул уперся в pm.max_children
TOUT_MIN = 20  # запросов за час убито по request_terminate_timeout
TOUT_HOT = 100
CRASH_MIN = 3  # воркеров за час упало с SIGSEGV и т.п.: одно падение в день бывает от расширения
CRASH_HOT = 10
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему
KERN_KEY = "ядро|php"
KERN_LABEL = "PHP на ноде (журнал ядра)"
CRASH_HINT = ("так выглядят попытки взлома через ошибки памяти PHP (php2shell) или битое расширение:"
              " обновить PHP, смотреть POST-запросы в эти минуты")


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
    sat, tout, crash = _i(p, "sat"), _i(p, "tout"), _i(p, "crash")
    if sat >= SAT_HOT or tout >= TOUT_HOT or crash >= CRASH_HOT:
        return 2
    if sat >= SAT_MIN * k or _i(p, "reached") >= REACHED_MIN * k or tout >= TOUT_MIN * k or crash >= CRASH_MIN * k:
        return 1
    return 0


def _crashing(p: dict, held: bool = False) -> bool:
    return _i(p, "crash") >= CRASH_MIN * (0.5 if held else 1)


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
    if _crashing(p, held):
        parts.append(f"воркеры падают: {_i(p, 'crash')} за час")
    return ", ".join(parts)


def kern(rep: dict) -> dict | None:
    """Падения php по журналу ядра ноды строкой как у пула: {"crash", "crash_d", "crash_last", "crash_ts"}"""
    k = (block(rep) or {}).get("kern")
    if not isinstance(k, dict):
        return None
    return {"crash": _i(k, "n"), "crash_d": _i(k, "n_d"), "crash_last": str(k.get("last") or "")[:300],
            "crash_ts": _i(k, "ts")}


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
    out, crash = [], False
    for x in pools(s.last_report or {}):
        k = key(x)
        h = k in held
        lvl = level(x, h)
        crash = crash or _crashing(x, h)
        if not lvl or k in muted:
            continue
        w = why(x, h)
        text = f"PHP-FPM {label(x)}: {w}"
        ex = CRASH_HINT if _crashing(x, h) else str(x.get("tout_last") or x.get("slow_last") or "").strip()
        if ex:
            text += f"\n↳ {ex[:200]}"
        out.append({"key": k, "label": label(x), "level": lvl, "why": w, "text": text,
                    # для короткого алерта: где, пул, предел воркеров, что сработало, сигнал падения
                    "where": where_text(str(x.get("where") or "")), "pool": str(x.get("pool") or ""),
                    "max": _i(x, "max"), "causes": _causes(x, h), "sig": _signal(str(x.get("crash_last") or ""))})
    # журнал ядра: падения, которые не объясняет пул (лог FPM не нашелся, падает не FPM)
    kx = kern(s.last_report or {})
    h = KERN_KEY in held
    if kx and not crash and KERN_KEY not in muted and level(kx, h):
        w = _kern_why(kx)
        out.append({"key": KERN_KEY, "label": KERN_LABEL, "level": level(kx, h), "why": w,
                    "text": f"{w}\n↳ {CRASH_HINT}", "kern": True, "causes": ["crash"],
                    "proc": _kern_proc(kx["crash_last"])})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


def _causes(p: dict, held: bool = False) -> list[str]:
    """Что сработало (как в why): sat - запросы ждали воркера, reached - упор в предел, tout - убиты
    по таймауту, crash - воркеры падают"""
    k = 0.5 if held else 1
    out = []
    if _i(p, "sat") >= SAT_MIN * k:
        out.append("sat")
    if _i(p, "reached") >= REACHED_MIN * k:
        out.append("reached")
    if _i(p, "tout") >= TOUT_MIN * k:
        out.append("tout")
    if _crashing(p, held):
        out.append("crash")
    return out


_SIG = {"11": "SIGSEGV", "6": "SIGABRT", "7": "SIGBUS", "4": "SIGILL", "8": "SIGFPE"}


def _signal(line: str) -> str:
    """Сигнал падения из строки лога FPM: "child 12 exited on signal 11 (SIGSEGV)" -> "SIGSEGV" """
    m = re.search(r"\((SIG[A-Z]+)\)", line)
    if m:
        return m.group(1)
    m = re.search(r"signal (\d+)", line)
    return _SIG.get(m.group(1), f"сигнал {m.group(1)}") if m else ""


def _kern_proc(line: str) -> str:
    """Процесс из строки журнала ядра: "php-fpm7.4[1234]: segfault at ..." -> "php-fpm7.4" """
    m = re.search(r"([\w.@+-]+)\[\d+\]", line)
    return m.group(1) if m else ""


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----

def _pool_where(x: dict) -> str:
    """"media" (пул на хосте), "www в shop-php" """
    w = str(x.get("where") or "")
    return str(x.get("pool") or "?") + ("" if w in ("", "на хосте") else " " + where_in(w))


def tg_short(fp: list[dict]) -> tuple[str, list[str]] | None:
    """Короткий алерт по problems(): "PHP-FPM не справляется / Пул: media, заняты все 50
    воркеров". Что - про худший пул (первый), остальные именами"""
    if not fp:
        return None
    x = fp[0]
    causes = x.get("causes") or []
    if x.get("kern"):
        proc = x.get("proc") or ""
        what, facts = "Падает PHP на ноде", [f"{proc}, segfault" if proc else "segfault по журналу ядра"]
    elif "sat" in causes or "reached" in causes:
        mx = int(x.get("max") or 0)
        busy = ("занят единственный воркер" if mx == 1 else
                f"заняты все {mx} {_plural(mx, 'воркер', 'воркера', 'воркеров')}" if mx else "заняты все воркеры")
        what, facts = "PHP-FPM не справляется", [tg_fmt.fact("Пул", f"{_pool_where(x)}, {busy}")]
        if "crash" in causes:
            facts.append("воркеры еще и падают" + (f" ({x['sig']})" if x.get("sig") else ""))
    elif "tout" in causes:
        what, facts = "PHP-FPM обрывает запросы по таймауту", [tg_fmt.fact("Пул", _pool_where(x))]
    else:
        sig = x.get("sig") or ""
        what, facts = "Падают воркеры PHP-FPM", [tg_fmt.fact("Пул", _pool_where(x) + (f", {sig}" if sig else ""))]
    rest = [_pool_where(y) for y in fp[1:] if not y.get("kern")]
    if rest:
        facts.append(tg_fmt.fact("Еще", tg_fmt.names(rest)))
    return what, facts[:2]


def _kern_why(kx: dict) -> str:
    return f"PHP на ноде падает: {kx['crash']} за час по журналу ядра (segfault)"


_INT = ("max", "children", "conns", "queue", "sat", "span", "reached", "reached_d", "slow", "slow_d", "tout",
        "tout_d", "crash", "crash_d", "slowlog", "terminate", "status", "slow_ts", "tout_ts", "crash_ts")


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
               "tout_last": str(x.get("tout_last") or "")[:300], "crash_last": str(x.get("crash_last") or "")[:300]}
        row.update({f: _i(x, f) for f in _INT})
        lvl = level(x, k in held)
        if lvl:
            row["level"], row["why"] = lvl, why(x, k in held)
        rows.append(row)
    # падения по журналу ядра - строкой в конце, когда за сутки были
    kx = kern(s.last_report or {})
    if kx and kx["crash_d"] > 0:
        row = {"key": KERN_KEY, "label": KERN_LABEL, "kern": True, "pool": "", "listen": "", "pm": "",
               "slow_last": "", "tout_last": ""}
        row.update({f: _i(kx, f) for f in _INT})
        row["crash_last"] = kx["crash_last"]
        lvl = level(kx, KERN_KEY in held)
        if lvl:
            row["level"], row["why"] = lvl, _kern_why(kx)
        rows.append(row)
    for row in rows:
        k = row["key"]
        if k in perm:
            row["mute"] = {"perm": True}
        elif k in muted and k in snz:
            row["mute"] = {"until": snz[k]}
    sk = [{"where": where_text(str(x.get("where") or "")), "why": str(x.get("why") or "")}
          for x in b.get("skipped") or [] if isinstance(x, dict)]
    return {"ts": int(b.get("ts") or 0), "pools": rows, "skipped": sk}
