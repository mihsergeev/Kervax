"""Воркеры под supervisor и pm2: упал, не запускается, крутится в перезапусках.

Воркер очереди, слушатель или демон под supervisord или pm2 не виден ни systemd, ни Docker:
supervisor перезапускает его сам, после нескольких неудачных запусков сдается (FATAL), и никто
не замечает, пока работа не встанет. Helper workers-setup раз в минуту спрашивает каждый
supervisord и pm2 ноды (Docker, поды, хост) об их программах: extras "workers" -
{"v": 1, "ts", "daemons": [{"kind": "supervisor" | "pm2", "where", "progs": [{"name", "state",
"since", "up", "pid", "exit", "r15", "r60", "desc", "log"}]}], "skipped": [{"where", "kind", "why"}]}.
r15 и r60 - сколько раз программа перезапускалась за 15 минут и за час (helper считает по смене
времени старта), log - последняя строка лога у упавшей или недавно запущенной.

Беда (problems):
- supervisor сдался (FATAL) или pm2 перестал поднимать (errored) - проблема: воркер не работает;
- BACKOFF дольше BACKOFF_S - проблема: supervisor все еще пробует, но запуститься не выходит;
- завершился с ненулевым кодом и не перезапускается (EXITED, autorestart=false) - предупреждение;
- работает, но перезапустился LOOP_MIN раз за 15 минут - предупреждение: падает и поднимается.
Остановленную руками (STOPPED, pm2 stopped) не судим: это решение человека.

Заглушить воркер можно насовсем (alert_mutes "worker:<ключ>") или на время (alert_snoozes с тем
же ключом); ключ - "Supervisor, контейнер app|queue-listen".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Server
from app.queue_watch import where_text

MUTE_PREFIX = "worker:"
BACKOFF_S = 180  # BACKOFF дольше трех минут - не переходное состояние, а не запускается
LOOP_MIN = 3  # перезапусков за 15 минут - падает и поднимается
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему
KIND_LABEL = {"supervisor": "Supervisor", "pm2": "PM2"}
_SKIP_WHY = {"auth": "не пустил по паролю из его конфига", "norpc": "у supervisord не включен сокет управления",
             "noconf": "не нашелся конфиг supervisord", "nolib": "не нашелся клиент pm2",
             "down": "не ответил"}


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("workers")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def daemons(rep: dict) -> list[dict]:
    """Демоны с программами: {"kind", "label", "where" (словами), "source" (часть ключа), "progs"}"""
    b = block(rep)
    out = []
    for d in (b or {}).get("daemons") or []:
        if not isinstance(d, dict):
            continue
        kind = str(d.get("kind") or "")
        label = KIND_LABEL.get(kind, kind)
        wt = where_text(str(d.get("where") or ""))
        progs = [p for p in d.get("progs") or [] if isinstance(p, dict) and p.get("name")]
        out.append({"kind": kind, "label": label, "where": wt, "source": f"{label}, {wt}", "progs": progs,
                    "ts": int(b.get("ts") or 0)})
    return out


def skipped(rep: dict) -> list[dict]:
    """Демоны, которых helper не смог спросить: {"where", "why"} словами"""
    b = block(rep)
    out = []
    for x in (b or {}).get("skipped") or []:
        if isinstance(x, dict):
            label = KIND_LABEL.get(str(x.get("kind") or ""), str(x.get("kind") or ""))
            out.append({"where": f"{label}, {where_text(str(x.get('where') or ''))}",
                        "why": _SKIP_WHY.get(str(x.get("why") or ""), str(x.get("why") or ""))})
    return out


def key(d: dict, p: dict) -> str:
    return f"{d['source']}|{p.get('name') or ''}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def trouble(d: dict, p: dict) -> tuple[int, str] | None:
    """(уровень, что с ним) или None, если все в порядке"""
    st = str(p.get("state") or "")
    exit_code = p.get("exit")
    if st == "FATAL":
        return 2, "не запускается, supervisor перестал его поднимать"
    if st == "errored":
        return 2, "не запускается, pm2 перестал его поднимать"
    if st == "BACKOFF" and int(d.get("ts") or 0) - int(p.get("since") or 0) >= BACKOFF_S:
        return 2, "не может запуститься, supervisor пробует снова и снова"
    if st == "EXITED" and isinstance(exit_code, int) and exit_code != 0:
        return 1, f"завершился с кодом {exit_code} и не перезапускается"
    r15 = int(p.get("r15") or 0)
    if r15 >= LOOP_MIN and st in ("RUNNING", "STARTING", "BACKOFF", "online", "launching"):
        return 1, f"падает и поднимается: {r15} {_plural(r15, 'перезапуск', 'перезапуска', 'перезапусков')} за 15 минут"
    return None


def muted_keys(s: Server, now: datetime) -> set[str]:
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


def _all(s: Server) -> list[dict]:
    out = []
    for d in daemons(s.last_report or {}):
        for p in d["progs"]:
            t = trouble(d, p)
            if t is None:
                continue
            lvl, why = t
            text = f"воркер {p['name']} ({d['source']}) {why}"
            log = str(p.get("log") or p.get("desc") or "").strip()
            if log:
                text += f"\n↳ {log[:200]}"
            since = int(p.get("since") or d["ts"] or 0)
            out.append({"key": key(d, p), "name": str(p["name"]), "source": d["source"], "level": lvl, "why": why,
                        "text": text, "since": datetime.fromtimestamp(since, timezone.utc).isoformat()})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


def problems(s: Server, now: datetime) -> list[dict]:
    """Воркеры в беде, без заглушенных: {"key", "name", "source", "level", "why", "text", "since"}"""
    muted = muted_keys(s, now)
    return [x for x in _all(s) if x["key"] not in muted]


def view(s: Server, now: datetime) -> list[dict] | None:
    """Для раздела "Воркеры": демоны с программами, у каждой "key", беда ("level", "why") и мьют
    ({"perm": True} | {"until": iso})"""
    ds = daemons(s.last_report or {})
    if not ds:
        return None
    muted = muted_keys(s, now)
    perm = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    snz = {str(k)[len(MUTE_PREFIX):]: str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()
           if str(k).startswith(MUTE_PREFIX)}
    out = []
    for d in ds:
        progs = []
        for p in d["progs"]:
            k = key(d, p)
            t = trouble(d, p)
            row = {"name": str(p["name"]), "state": str(p.get("state") or ""), "up": int(p.get("up") or 0),
                   "since": int(p.get("since") or 0), "r15": int(p.get("r15") or 0), "r60": int(p.get("r60") or 0),
                   "exit": p.get("exit") if isinstance(p.get("exit"), int) else None, "key": k}
            if p.get("log") or p.get("desc"):
                row["log"] = str(p.get("log") or p.get("desc"))[:300]
            if t is not None:
                row["level"], row["why"] = t
            if k in perm:
                row["mute"] = {"perm": True}
            elif k in muted and k in snz:
                row["mute"] = {"until": snz[k]}
            progs.append(row)
        out.append({"kind": d["kind"], "label": d["label"], "where": d["where"], "source": d["source"],
                    "ts": d["ts"], "progs": progs})
    return out
