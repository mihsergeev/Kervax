"""Настроен ли на сервере бэкап - одно правило для главной, раздела "Бэкапы", алертов и формы
"Настроить бэкап".

До 1.4.320 каждое место решало по-своему: раздел считал живым бэкап с таймером или с restic и
метрикой, главная и алерт "бэкап завершился с ошибкой" верили одной метрике, а форма отказывала,
если есть хоть метрика. Нода с метрикой, оставшейся от старого бэкапа (restic и таймера давно
нет), была одновременно "без бэкапа", "бэкап упал" и "бэкап уже настроен" - настроить его
заново было нельзя.

Состояния (view):
- ok - бэкап есть: таймер бэкапа или restic вместе с метрикой (backup_alive). Упал ли прогон и
  почему - fail_reason;
- partial - бэкапа нет, а последняя настройка из панели упала на полпути (servers.backup_setup_err):
  на клиенте могли остаться restic, env и юниты. Чинится "переделать" или "удалить настройку";
- none - бэкапа нет. traces - что осталось от старого (метрика без restic и таймера), чтобы было
  понятно, откуда взялась прежняя ошибка.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import Server

# helper backup-setup пишет ошибку прогона в backup-runs.json с этой версии
FAIL_REASON_HELPER = "0.39"
# ошибка из backup-runs.json относится к последнему прогону, если записана не раньше чем за
# столько секунд до метрики (метрика пишется в конце прогона, разбор журнала - после)
_FAIL_SLACK = 600

# отказ "настроить бэкап" без согласия переустановить поверх живого. Ожидаемый (форма уже знает
# про бэкап, а список мог устареть), журнал ошибок панели его не пишет
BACKUP_EXISTS = 'на этом сервере уже есть бэкап - отметьте "переустановить поверх"'


def backup_alive(bk: dict | None) -> bool:
    """Есть ли на ноде бэкап: таймер бэкапа или restic, который хоть раз бэкапил. Метрика без
    restic и таймера - следы снесенного бэкапа, а не бэкап. Тот же расчет - backupAlive на фронте."""
    bk = bk or {}
    if not bk.get("present"):
        return False
    return bool(bk.get("configured")) or (bool(bk.get("restic_found")) and bool(bk.get("metric_present")))


def backup_failed(bk: dict | None) -> bool:
    """Последний прогон живого бэкапа упал: по метрике, а без нее - по Result юнита."""
    bk = bk or {}
    if not backup_alive(bk):
        return False
    if bk.get("ts_source") == "systemd":
        return (bk.get("service_result") or "").strip() not in ("", "success")
    return bool(bk.get("metric_present")) and bk.get("success") == 0


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y")


def traces(bk: dict | None) -> str:
    """Что осталось от старого бэкапа на ноде без бэкапа: пусто, если ничего."""
    bk = bk or {}
    if backup_alive(bk) or not bk.get("metric_present"):
        return ""
    ts = bk.get("last_backup_ts") or 0
    when = f" от {_day(ts)}" if ts else ""
    return f"осталась метрика старого бэкапа{when}, restic и таймера бэкапа нет"


def fail_reason(rep: dict | None) -> str:
    """Почему упал последний прогон: строка ошибки restic из журнала (helper backup-setup 0.39,
    report.d/backup-runs.json, поле fail). Пусто - прогон не падал или helper старый."""
    rep = rep or {}
    bk = rep.get("backup") or {}
    if not backup_failed(bk):
        return ""
    runs = (rep.get("extras") or {}).get("backup-runs") or {}
    fail = runs.get("fail") if isinstance(runs, dict) else None
    if not isinstance(fail, dict):
        return ""
    msg = str(fail.get("msg") or "").strip()
    ts = float(fail.get("ts") or 0)
    last = float(bk.get("last_backup_ts") or 0)
    # ошибка прошлого прогона, а последний упал иначе (или метрика из systemd без времени)
    if not msg or (last and ts and ts < last - _FAIL_SLACK):
        return ""
    return msg[:300]


def setup_error(s: Server) -> dict | None:
    """Последняя настройка из панели упала: {ts, msg, job}. Пока сервер без бэкапа."""
    err = getattr(s, "backup_setup_err", None)
    if not isinstance(err, dict) or backup_alive((s.last_report or {}).get("backup")):
        return None
    return err


def view(s: Server) -> dict:
    """Состояние бэкапа для фронта: {state, reason, since, traces}. reason - почему: упавший прогон
    (ok) или упавшая настройка (partial); since - с какого момента (ISO) для partial."""
    rep = s.last_report or {}
    bk = rep.get("backup") or {}
    if backup_alive(bk):
        return {"state": "ok", "reason": fail_reason(rep), "since": None, "traces": ""}
    err = setup_error(s)
    if err:
        return {"state": "partial", "reason": str(err.get("msg") or ""), "since": err.get("ts"),
                "traces": traces(bk)}
    return {"state": "none", "reason": "", "since": None, "traces": traces(bk)}
