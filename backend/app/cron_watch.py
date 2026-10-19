"""Задачи cron на хосте и в контейнерах: наслаиваются, висят, их вывод выбрасывается.

Задачу, запущенную cron, не видят ни systemd, ни Docker. Зависла - cron по расписанию запускает
рядом следующую, копии копятся и держат память, соединения и блокировки; задача под flock висит
одна, и ее работа молча встает. Helper cron-setup раз в минуту находит демоны cron ноды (cron и
cronie, busybox crond, supercronic; хост, контейнеры, поды), читает их crontab и смотрит, какие
задачи идут сейчас: extras "cron" - {"v": 1, "ts", "journal", "disc24", "daemons": [{"where",
"kind", "mta", "jobs"}], "jobs": [{"where", "kind", "user", "sched", "src", "cmd", "last", "runs",
"max", "done", "n", "lost", "order", "run": [{"pid", "age", "cpu", "procs"}]}]}.
last - последний запуск (журнал хоста или замеченная копия), runs - запусков за сутки по журналу
(-1 - журнала нет, как в контейнере), max - самая долгая из последних done законченных копий
(секунды, сколько ее видели), n - сколько копий идет сейчас, run - самые старые из них: возраст,
CPU копии со всеми ее детьми в секундах, процессов. lost - вывод задачи доходит до cron, а почты
нет ("err", "out", "both"), order - stderr ушел в cron из-за 2>&1, записанного раньше
перенаправления stdout в файл. disc24 - сколько раз за сутки cron хоста выбросил вывод.

Беда (problems), только то, на что надо реагировать:
- наслоение: идут 2+ копии, и вторая по старшинству идет дольше OVERLAP_S. Новая копия
  стартовала, пока старая не кончилась, и это не мимолетно: задача раз в минуту, которая иногда
  работает 70 секунд, не в счет. PILE копий и больше - проблема, иначе предупреждение;
- висит: самая старая копия идет в разы дольше обычного - дольше max(HUNG_MIN, 3 x обычное,
  обычное + час), обычное - самая долгая из законченных. Законченные видели (DONE_MIN), и все
  короче минуты - предел HUNG_MIN; истории нет - HUNG_NOBASE. Копии крутят процессор (в среднем
  ядро и больше) - проблема, иначе предупреждение.
После ушедшего алерта наслоение держится, пока вторая копия старше OVERLAP_S / 2.

Заглушить задачу можно насовсем (alert_mutes "cron:<ключ>") или на время (alert_snoozes с тем же
ключом); ключ - "<где>|<команда>", например "на хосте|/lib65/scripts/sync.sh".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models import Server
from app.queue_watch import where_text

MUTE_PREFIX = "cron:"
OVERLAP_S = 900  # вторая копия идет четверть часа - наслоение не мимолетное
PILE = 5  # копий одной задачи - копятся, проблема
HUNG_MIN = 3600  # висит: не раньше часа
HUNG_NOBASE = 6 * 3600  # истории нет - только совсем долгое
DONE_MIN = 3  # законченных копий, чтобы считать задачу короткой
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему
BOGUS_S = 400 * 86400  # копия или длительность дольше - мусор ps, а не задача
KIND_LABEL = {"cron": "cron", "busybox": "busybox crond", "supercronic": "supercronic"}


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("cron")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def _sane_age(r: dict, limit: float) -> dict:
    """Копия старше аптайма ноды не бывает: это etimes ps из procps-ng 4.0.4 у процесса, который
    родился, пока ps читал /proc (4123168608, "висит 47721 сут"). Такая копия только что началась."""
    try:
        age = int(r.get("age") or 0)
    except (TypeError, ValueError):
        age = 0
    return {**r, "age": 0} if age > limit else r


def jobs(rep: dict) -> list[dict]:
    """Задачи: как в отчете плюс "source" (где, словами) и "key" (для мьюта)"""
    out = []
    up = float((rep or {}).get("uptime_seconds") or 0)
    limit = up + 900 if up > 0 else BOGUS_S
    for j in (block(rep) or {}).get("jobs") or []:
        if not isinstance(j, dict) or not j.get("cmd"):
            continue
        src = where_text(str(j.get("where") or ""))
        row = {**j, "source": src, "key": f"{src}|{j['cmd']}",
               "run": [_sane_age(r, limit) for r in j.get("run") or [] if isinstance(r, dict)]}
        # тот же мусор, запомненный helper'ом 0.1 как длительность законченной копии: "обычно до
        # 47721 сут" и предел "висит" в 130 лет на месяц, пока запись не выйдет из кэша
        try:
            if int(j.get("max") or 0) > BOGUS_S:
                row["max"] = 0
        except (TypeError, ValueError):
            row["max"] = 0
        out.append(row)
    return out


def _dur(sec: int) -> str:
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400} сут {sec % 86400 // 3600} ч"
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{max(sec, 1)} с"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def usual(j: dict) -> tuple[int, int]:
    """(предел, после которого копия висит; обычная длительность: секунды, 1 - "меньше минуты",
    0 - неизвестна)"""
    mx = int(j.get("max") or 0)
    done = int(j.get("done") or 0)
    if mx >= 60 and done > 0:
        return max(HUNG_MIN, 3 * mx, mx + 3600), mx
    if done >= DONE_MIN:
        return HUNG_MIN, 1
    return HUNG_NOBASE, 0


def trouble(j: dict, held: bool = False) -> tuple[int, str] | None:
    """(уровень, что с задачей) или None. held - алерт о ней уже ушел: отбой наслоения с запасом"""
    run = sorted(j.get("run") or [], key=lambda r: -int(r.get("age") or 0))
    if not run:
        return None
    n = max(int(j.get("n") or 0), len(run))
    oldest = int(run[0].get("age") or 0)
    cpu = sum(int(r.get("cpu") or 0) for r in run)
    limit, typ = usual(j)
    parts, lvl = [], 0
    second = int(run[1].get("age") or 0) if len(run) > 1 else 0
    if n >= 2 and second >= (OVERLAP_S // 2 if held else OVERLAP_S):
        lvl = 2 if n >= PILE else 1
        parts.append(f"наслаивается: идут {n} {_plural(n, 'копия', 'копии', 'копий')}, самая старая {_dur(oldest)}")
    if oldest >= limit:
        spin = oldest > 0 and cpu / oldest >= 1
        lvl = max(lvl, 2 if spin else 1)
        if not parts:
            parts.append(f"висит {_dur(oldest)}")
        parts.append("обычно меньше минуты" if typ == 1 else f"обычно до {_dur(typ)}" if typ else "")
    if not parts:
        return None
    if lvl and cpu >= 60:
        parts.append(f"съела {_dur(cpu)} процессора")
    return lvl, ", ".join(p for p in parts if p)


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
    return {str(k) for k in ((s.alert_state or {}).get("cron_stuck_sig") or [])}


def _all(s: Server) -> list[dict]:
    held = _held(s)
    out = []
    ts = int((block(s.last_report or {}) or {}).get("ts") or 0)
    for j in jobs(s.last_report or {}):
        t = trouble(j, j["key"] in held)
        if t is None:
            continue
        lvl, why = t
        sched = str(j.get("sched") or "")
        text = f"крон {j['source']}{' (' + sched + ')' if sched else ''} {why}\n↳ {str(j['cmd'])[:200]}"
        oldest = max(int(r.get("age") or 0) for r in j["run"])
        out.append({"key": j["key"], "source": j["source"], "level": lvl, "why": why, "text": text,
                    "since": datetime.fromtimestamp(max(ts - oldest, 0), timezone.utc).isoformat()})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


def problems(s: Server, now: datetime) -> list[dict]:
    """Задачи в беде, без заглушенных: {"key", "source", "level", "why", "text", "since"}"""
    muted = _muted(s, now)
    return [x for x in _all(s) if x["key"] not in muted]


def view(s: Server, now: datetime) -> dict | None:
    """Для раздела "Кроны": {"ts", "journal", "disc24", "daemons", "jobs"}; у задачи "key", беда
    ("level", "why"), обычная длительность ("usual": секунды, 1 - меньше минуты) и мьют
    ({"perm": True} | {"until": iso})"""
    b = block(s.last_report or {})
    if b is None:
        return None
    muted = _muted(s, now)
    held = _held(s)
    perm = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    snz = {str(k)[len(MUTE_PREFIX):]: str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()
           if str(k).startswith(MUTE_PREFIX)}
    rows = []
    for j in jobs(s.last_report or {}):
        k = j["key"]
        row = {"where": j["source"], "kind": KIND_LABEL.get(str(j.get("kind") or ""), str(j.get("kind") or "")),
               "user": str(j.get("user") or ""), "sched": str(j.get("sched") or ""), "src": str(j.get("src") or ""),
               "cmd": str(j["cmd"]), "last": int(j.get("last") or 0), "runs": int(j.get("runs") if j.get("runs") is not None else -1),
               "n": int(j.get("n") or 0), "run": j["run"][:5], "usual": usual(j)[1], "key": k}
        if j.get("lost") in ("err", "out", "both"):
            row["lost"] = j["lost"]
            if j.get("order"):
                row["order"] = True
        t = trouble(j, k in held)
        if t is not None:
            row["level"], row["why"] = t
        if k in perm:
            row["mute"] = {"perm": True}
        elif k in muted and k in snz:
            row["mute"] = {"until": snz[k]}
        rows.append(row)
    ds = [{"where": where_text(str(d.get("where") or "")), "kind": KIND_LABEL.get(str(d.get("kind") or ""), str(d.get("kind") or "")),
           "mta": bool(d.get("mta")), "jobs": int(d.get("jobs") or 0)}
          for d in b.get("daemons") or [] if isinstance(d, dict)]
    return {"ts": int(b.get("ts") or 0), "journal": bool(b.get("journal")), "disc24": int(b.get("disc24") or 0),
            "daemons": ds, "jobs": rows}
