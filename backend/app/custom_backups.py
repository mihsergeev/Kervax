"""Свои бэкапы ноды: заданные без панели cron-задания, systemd-таймеры и скрипты с метриками.

Сервер обычно приходит в панель уже со своими бэкапами: pg_dump по крону, таймер с дампом
Neo4j, ансибл-роль, пишущая метрики для node_exporter. Переделывать их под панель незачем,
поэтому панель за ними только СЛЕДИТ. Находит их root-helper (backup-setup, custom-scan) и
кладёт в отчёт агента блок extras["custom-backups"] — без командных строк и тел скриптов,
только имена, расписания, времена, размеры, пути и имена контейнеров. Здесь из этого
получается статус: работает, упал, давно не отрабатывал, не понять.
"""

from datetime import datetime

from app.schemas import CustomBackupDb, CustomBackupOut

EXTRA_KEY = "custom-backups"

# Самый редкий разумный бэкап — ежемесячный; реже этого расписанию не верим.
_MAX_INTERVAL = 31 * 86400
_DAY = 86400


# Сведения helper'а устарели: он обновляет их раз в 5 минут, и три часа тишины значат, что
# он больше не запускается. Судить по застывшему снимку нельзя — через двое суток он дал бы
# «нет свежего бэкапа» по заданиям, которые на самом деле исправно работают.
_SCAN_STALE = 3 * 3600


def _block(server) -> dict:
    rep = getattr(server, "last_report", None) or {}
    block = ((rep.get("extras") or {}).get(EXTRA_KEY)) or {}
    return block if isinstance(block, dict) else {}


def jobs_of(server) -> list[dict]:
    jobs = _block(server).get("jobs")
    return [j for j in (jobs or []) if isinstance(j, dict) and j.get("id")]


def cron_interval(sched: str) -> int:
    """Грубая оценка интервала cron-расписания (5 полей или @daily) в секундах."""
    s = (sched or "").strip()
    macros = {"@hourly": 3600, "@daily": _DAY, "@midnight": _DAY, "@weekly": 7 * _DAY,
              "@monthly": 28 * _DAY, "@yearly": 365 * _DAY, "@annually": 365 * _DAY}
    if s in macros:
        return macros[s]
    p = s.split()
    if len(p) != 5:
        return _DAY
    minute, hour, dom, _mon, dow = p

    def step(v: str) -> int:
        try:
            return max(int(v.split("/", 1)[1]), 1)
        except (IndexError, ValueError):
            return 1

    if minute == "*":
        return 60
    if minute.startswith("*/"):
        return step(minute) * 60
    if hour == "*":
        return 3600
    if hour.startswith("*/"):
        return step(hour) * 3600
    if "," in hour:
        return max(_DAY // (hour.count(",") + 1), 3600)
    if dow != "*":
        return 7 * _DAY
    if dom.startswith("*/"):
        return step(dom) * _DAY
    if dom != "*":
        return 28 * _DAY
    return _DAY


def calendar_interval(cal: str) -> int:
    """Грубая оценка интервала systemd OnCalendar."""
    c = (cal or "").strip().lower()
    named = {"minutely": 60, "hourly": 3600, "daily": _DAY, "weekly": 7 * _DAY,
             "monthly": 28 * _DAY, "quarterly": 90 * _DAY, "yearly": 365 * _DAY,
             "annually": 365 * _DAY}
    if c in named:
        return named[c]
    if not c:
        return _DAY
    parts = c.split()
    # "Mon *-*-* 03:00:00" / "Mon..Fri ..." — день недели в начале
    if parts and parts[0][:3] in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        return 7 * _DAY if "," not in parts[0] and ".." not in parts[0] else _DAY
    tm = parts[-1] if ":" in parts[-1] else ""
    date = parts[0] if len(parts) > 1 or "-" in parts[0] else "*-*-*"
    hh = tm.split(":")[0] if tm else ""
    if hh in ("*",) or hh.startswith("*/"):
        return 3600
    if "," in hh:
        return max(_DAY // (hh.count(",") + 1), 3600)
    d = date.split("-")
    if len(d) == 3 and d[2] not in ("*",) and not d[2].startswith("*/"):
        return 28 * _DAY
    return _DAY


def _interval(job: dict) -> int:
    kind = job.get("kind")
    if kind == "systemd":
        nxt, prev = int(job.get("next_ts") or 0), int(job.get("prev_ts") or 0)
        if nxt > prev > 0:
            return min(max(nxt - prev, 3600), _MAX_INTERVAL)
        return min(calendar_interval(job.get("schedule") or ""), _MAX_INTERVAL)
    if kind == "cron":
        return min(cron_interval(job.get("schedule") or ""), _MAX_INTERVAL)
    return _DAY  # одни метрики без расписания: считаем ежедневным


def stale_after(job: dict) -> int:
    """Когда молчание — уже проблема: два интервала расписания, но не меньше двух часов.
    Ежедневный бэкап имеет право не отработать одну ночь (перезагрузка, окно обслуживания),
    а вот второй пропуск подряд — это уже поломка, а не случайность."""
    return max(_interval(job) * 2, 2 * 3600)


def _ago(seconds: float) -> str:
    if seconds < 2 * 3600:
        return f"{max(int(seconds // 60), 1)} мин"
    if seconds < 2 * _DAY:
        return f"{int(seconds // 3600)} ч"
    return f"{seconds / _DAY:.1f} дн".replace(".0 ", " ")


def evaluate(job: dict, now: datetime, ignored: set[str], fresh: bool = True) -> CustomBackupOut:
    nowts = now.timestamp()
    ok = int(job.get("ok") if job.get("ok") is not None else -1)
    ok_ts = int(job.get("ok_ts") or 0)
    run_ts = int(job.get("run_ts") or 0)
    after = stale_after(job)
    status, problem = "unknown", ""
    if job.get("id") in ignored:
        status = "ignored"
    elif not fresh:
        pass  # снимок застыл: ни «работает», ни «упал» по нему утверждать нельзя
    elif job.get("running"):
        status = "running"
    elif job.get("kind") == "systemd" and job.get("enabled") is False:
        status, problem = "disabled", "таймер выключен — бэкап не запускается"
    elif ok == 0:
        fail = job.get("fail") or ""
        if fail == "state":
            status, problem = "failed", "скрипт отметил последний запуск как неудачный"
        elif fail == "state_old":
            # скрипт пишет итог после каждого прогона, а после последнего не записал ничего
            status, problem = "failed", "последний запуск не записал итог — скрипт, похоже, прервался"
        elif fail == "restic":
            status, problem = "failed", "последний запуск не сохранил снимок restic"
        else:
            res = (job.get("result") or "").strip()
            why = {"timeout": "не уложился в таймаут", "exit-code": "завершился с ошибкой",
                   "signal": "убит сигналом", "core-dump": "упал"}.get(res, "завершился с ошибкой")
            status, problem = "failed", f"последний прогон {why}"
        bad = [d.get("name") for d in (job.get("dbs") or []) if d.get("ok") == 0]
        if bad:
            problem += ": " + ", ".join(str(b) for b in bad[:5])
    elif ok_ts > 0:
        age = nowts - ok_ts
        if age > after:
            status, problem = "stale", f"нет свежего бэкапа {_ago(age)}"
        else:
            status = "ok"
    elif ok == 1:
        status = "ok"
    elif job.get("kind") == "cron" and job.get("run_src") == "journal" and run_ts > 0:
        # Итога не видно, но запуски видны по журналу cron. Тишина в журнале — это уже
        # не «не понять», а поломка: cron не запускает задание (снят, сломан, нода лежала).
        # Время записи лога так не читаем: скрипт может писать в лог только при ошибке.
        age = nowts - run_ts
        if age > after:
            status, problem = "stale", f"cron не запускал задание {_ago(age)}"
        else:
            status = "ran"
    dbs = [
        CustomBackupDb(
            name=str(d.get("name") or "")[:120], ok=int(d.get("ok") if d.get("ok") is not None else -1),
            ts=int(d.get("ts") or 0), size_bytes=int(d.get("size_bytes") or 0),
        )
        for d in (job.get("dbs") or [])[:30] if isinstance(d, dict)
    ]
    return CustomBackupOut(
        id=str(job.get("id"))[:120],
        kind=str(job.get("kind") or "")[:16],
        name=str(job.get("name") or "")[:200],
        desc=str(job.get("desc") or "")[:200],
        schedule=str(job.get("schedule") or "")[:120],
        engines=[str(e)[:40] for e in (job.get("engines") or [])][:8],
        files=bool(job.get("files")),
        containers=[str(c)[:120] for c in (job.get("containers") or [])][:8],
        status=status,
        problem=problem,
        ok_ts=ok_ts,
        run_ts=run_ts,
        next_ts=int(job.get("next_ts") or 0),
        size_bytes=int(job.get("size_bytes") or 0),
        duration_sec=int(job.get("duration_sec") or 0),
        dir=str(job.get("dir") or "")[:300],
        log=str(job.get("log") or "")[:300],
        script=str(job.get("script") or "")[:300],
        unit=str(job.get("unit") or "")[:200],
        result=str(job.get("result") or "")[:40],
        metrics=str(job.get("metrics") or "")[:300],
        run_src=str(job.get("run_src") or "")[:16],
        state=str(job.get("state") or "")[:300],
        evidence=str(job.get("evidence") or "")[:16],
        fail=str(job.get("fail") or "")[:16],
        dbs=dbs,
        stale_after=after,
        ignored=status == "ignored",
        scan_stale=not fresh,
    )


def views(server, now: datetime) -> list[CustomBackupOut]:
    ignored = set(getattr(server, "custom_backup_ignored", None) or [])
    scanned = int(_block(server).get("ts") or 0)
    fresh = now.timestamp() - scanned <= _SCAN_STALE
    return [evaluate(j, now, ignored, fresh) for j in jobs_of(server)]


_PROBLEM = ("failed", "stale", "disabled")


def problems(server, now: datetime) -> list[str]:
    """«имя (что не так)» по заданиям, требующим внимания. Для алерта и главной."""
    return sorted(
        f"{v.name} ({v.problem})" for v in views(server, now) if v.status in _PROBLEM
    )


def has_file_backup(server, now: datetime) -> bool:
    """Есть ли свой файловый бэкап ноды (restic/borg/rsync…): тогда «бэкап не настроен»
    неправда. Дамп одной базы бэкапом всей ноды не считается."""
    return any(v.files and v.status != "ignored" for v in views(server, now))


def label(v: CustomBackupOut) -> str:
    where = {"cron": "cron", "systemd": "таймер", "metrics": "метрики"}.get(v.kind, v.kind)
    return f"{v.name} ({where}{f' {v.schedule}' if v.schedule else ''})"


def db_cover(server, now: datetime, engine: str, instance: str, instances: int):
    """Задание, которое бэкапит этот экземпляр СУБД, или None.

    Экземпляр — имя контейнера (пусто — нативная установка или под). Засчитываем, если
    задание явно ссылается на этот контейнер; без ссылок — только когда такой базы на
    ноде ровно одна. Иначе дамп одной postgres закрывал бы находку по второй — ровно та
    ошибка, из-за которой раньше молча пропадали из аудита непокрытые базы."""
    for v in views(server, now):
        if v.status == "ignored" or engine not in v.engines:
            continue
        if instance and instance in v.containers:
            return v
        if not v.containers and (not instance or instances == 1):
            return v
    return None
