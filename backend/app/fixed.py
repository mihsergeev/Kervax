"""Починили: человек говорит, что проблему устранил, и алерт закрывается сейчас, а не по выдержке.

Чем это не известная проблема (app/known.py). Известная - "знаем, не буди, пока не станет хуже":
алерт молчит, а беда висит. Починили - беды больше нет, а алерт держит только счет за окно или
выдержка отбоя: контейнер перезапускался 11 раз за сутки, его поправили, а "11 раз за сутки"
висело бы до завтра.

Что делает отметка, по видам:
- WINDOW - беда считается по событиям за окно (перезапуски за сутки, OOM, юнит в цикле, 5xx за
  15 минут, авария в логе, упавшие Job, ошибки ввода-вывода). Счет начинается с отметки: что было
  до нее, больше не в счет ни в алерте, ни в "Что сломано". Повторилось и набрался порог снова -
  алерт уходит как обычно, без ожидания;
- STATE - беда это то, что сейчас (диск полон, юнит лежит, поды не поднимаются). Если панель и
  сейчас это видит - отметка не ставится, человеку говорят, что еще не починено и что именно
  видно. Если ушло, а алерт держит выдержка отбоя (_SETTLE_S, удержание юнитов и подов,
  гистерезис) - закрывается сразу.
Остальное (сроки, бэкапы, проверки доступа и портов, недоступность) кнопки не имеет: там нечего
считать заново, а "починено" скажет сама следующая проверка.

Запись в servers.alert_fixed: {вид: {at, by, text, done}}. done ставит сборщик, когда закрыл
алерт (отбой с пометкой, кто отметил). Отметка живет LIFE: дольше самого длинного окна ей
незачем."""

from __future__ import annotations

from datetime import datetime, timedelta

LIFE = 86400
# отказ API, когда беда еще видна (ожидаемый, не ошибка панели: app/panel_errors.py)
NOT_FIXED = "Еще не починено: {left}"

WINDOW = frozenset({
    "docker_restarts", "kube_restarts", "oom", "unit_loop", "docker_loop", "web_5xx", "log_errors",
    "kube_job", "disk_health",
})
STATE = frozenset({
    "cpu", "mem", "temp", "throttle", "conntrack", "db_conn", "disktemp", "disk", "inode", "pressure",
    "net_health", "link", "disk_slow", "dns", "clock", "units", "kube_pod", "kube_node", "flux_down",
    "flux_stale", "kube_cron", "listen_gone", "cron_stuck", "queue_stuck", "queue_failed", "workers", "fpm",
    "mem_limit", "gpu", "gpu_heat", "llm", "cpu_spin", "db_health", "web_config", "gre_down", "autoban",
    "vpn_torrent", "vpn_heavy",
})
KINDS = WINDOW | STATE
# события без отбоя (app/oom.py, упавший Job): закрывать в чате нечего, отметка только начинает счет
# заново и снимает придержанный повтор
NO_RECOVERY = frozenset({"oom", "kube_job"})


def _ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def entry(s, kind: str) -> dict | None:
    ent = (getattr(s, "alert_fixed", None) or {}).get(kind)
    return ent if isinstance(ent, dict) else None


def after(s, kind: str) -> datetime | None:
    """С какого момента считать события вида: отметка "починили" или None."""
    ent = entry(s, kind)
    return _ts(ent.get("at")) if ent else None


def after_ts(s, kind: str) -> float:
    a = after(s, kind)
    return a.timestamp() if a else 0.0


def pending(s, kind: str) -> bool:
    """Отметку поставили, а сборщик алерт еще не закрыл: объявленное раньше больше не держит
    пониженный порог ("до половины порога, пока алерт открыт")."""
    ent = entry(s, kind)
    return ent is not None and not ent.get("done")


def web_after(servers) -> dict[int, datetime]:
    """{id сервера: отметка} для окна ошибок 5xx (collector.web_error_window)."""
    out = {}
    for s in servers:
        a = after(s, "web_5xx")
        if a is not None:
            out[s.id] = a
    return out


def new_entry(text: str, by: str, now: datetime) -> dict:
    return {"text": text.strip(), "by": by, "at": now.isoformat(), "done": None}


def expired(ent: dict, now: datetime) -> bool:
    at = _ts(ent.get("at"))
    return at is None or (now - at).total_seconds() >= LIFE


def label(kind: str) -> str:
    from app import settings_store

    return settings_store.SERVER_ALERT_KINDS.get(kind, (kind,))[0]


def recovery_text(kind: str, ent: dict) -> str:
    """Отбой в чат: "Docker: часто перезапускается - починили (ivan: обновил образ)"."""
    label_ = label(kind)
    who = str(ent.get("by") or "")
    note = str(ent.get("text") or "").strip()
    tail = f"{who}: {note}" if who and note else who or note
    return f"{label_} - починили" + (f" ({tail[:120]})" if tail else "")


def broken(kind: str, lvl: int, ctx: dict) -> bool:
    """Есть ли беда по условию сборщика, а не только выдержка отбоя (collector._held_only, час
    чистоты у 5xx). raw - заполнение диска прямо сейчас, без выдержки."""
    from app import collector

    if "raw" in ctx:
        return int(ctx.get("raw") or 0) > 0
    if lvl <= 0 or collector._held_only(ctx):
        return False
    return not (kind == "web_5xx" and ctx.get("since"))


# что держит объявленный алерт в состоянии: проверка "починено ли" судит так, как будто его нет
_TOLD = ("", "_sig", "_told", "_told_lvl", "_hold", "_cool", "_calm", "_hush")


def left(s, kind: str, now: datetime, web_err=None, pod_names=None) -> str:
    """Что панель видит сейчас по этому виду, если считать с отметки (её уже записали в s) и
    алерта бы не было: пусто - не видит ничего, починено. "Что сломано" держит объявленное до
    отбоя (диск, юниты), условие сборщика - выдержкой, поэтому судим без объявленного."""
    from app import collector

    saved = s.alert_state
    drop = {kind + suf for suf in _TOLD}
    s.alert_state = {k: v for k, v in (saved or {}).items() if k not in drop}
    try:
        items = [p for p in collector.server_problems(s, now, pod_names, web_err, keep_fixed=True)
                 if p.get("kind") == kind]
        lvl, ctx = collector._server_conditions(s, now, web_err, pod_names).get(kind, (0, {}))
    finally:
        s.alert_state = saved
    if items:
        return "; ".join(str(p.get("text") or "") for p in items[:2])
    if lvl <= 0 and int(ctx.get("raw") or 0) <= 0 and (kind == "web_5xx" or not ctx.get("since")):
        return ""
    detail = str(ctx.get("detail") or "")
    if detail:
        return detail.split("\n")[0]
    unit, val = collector._SRV_UNIT.get(kind), ctx.get("value")
    label = collector._SRV_LABEL.get(kind, kind)
    return f"{label} {val}{unit or ''}" if val is not None else label


async def mark(session, s, kind: str, by: str, text: str, now: datetime) -> str:
    """Поставить отметку: пусто - поставлена, иначе что еще не починено (отметки нет).
    Известная проблема того же вида снимается: про беду, которой нет, знать нечего."""
    from app import collector

    if kind not in KINDS:
        return "этот алерт так не закрывается"
    old = s.alert_fixed
    cur = dict(old or {})
    cur[kind] = new_entry(text, by, now)
    s.alert_fixed = cur
    web_err = None
    if kind == "web_5xx":
        web_err = (await collector.web_error_window(session, now, {s.id: now})).get(s.id)
    try:
        still = left(s, kind, now, web_err)
    except Exception:  # noqa: BLE001 - не смогли проверить: не врем, что починено
        collector.log.warning("починили: не проверил %s у %s", kind, s.name, exc_info=True)
        still = "не получилось проверить, попробуйте через минуту"
    if still:
        s.alert_fixed = old
        return still
    kn = dict(s.known_issues or {})
    if kn.pop(kind, None) is not None:
        s.known_issues = kn or None
    return ""


def io_counts(s, now: datetime, last_io: datetime | None = None) -> tuple[bool, bool]:
    """Ошибки ввода-вывода (disk_health) с отметкой: (считать ли окно helper'а в 10 минут, держать ли
    час после прошлой пачки). Окно helper'а целиком после отметки - через 10 минут; пачка до отметки
    часа не держит."""
    a = after(s, "disk_health")
    if a is None:
        return True, True
    return now - a >= timedelta(minutes=10), last_io is None or last_io > a
