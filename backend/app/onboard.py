"""Подключение нового сервера.

Повод: через 15 минут тишины после добавления (collector._NEW_SERVER_QUIET) от сервера разом
приходило все, что было на нем задолго до панели: юнит, упавший 241 день назад, сертификат,
истекший год назад, 23 репозитория бэкапов, docker-сокет у прокси. С одной ноды 9 алертов за
полчаса, и ни один не про то, что сломалось сейчас.

Пока подключение не закрыто (человеком в карточке или само через ONBOARD_MAX от первого отчета
агента):
- алертом уходят только офлайн и критичный диск: про них надо знать сразу;
- пороговые проблемы придерживаются без записи состояния: они видны в карточке и в "Что
  сломано", но в Telegram не идут; что починилось само за это время (шум установки), так и не
  придет;
- события (перезагрузка, новый порт, доступ, бэкап-сервер) не отправляются, но их отметка
  двигается, как будто сообщение ушло: после закрытия старое не придет;
- при закрытии то, что все еще сломано, становится известной проблемой ("было при
  подключении", app/known.py): алерт придет, если станет хуже или сломается новое."""

from datetime import datetime, timedelta, timezone

ONBOARD_MAX = timedelta(days=3)
KNOWN_TEXT = "было при подключении"
# виды, которые уходят алертом и во время подключения
_SEND = frozenset({"offline", "disk", "inode"})
# их отметки в состоянии остаются в очереди вместе с сообщением (запишутся, когда дошло)
_SEND_STATE = frozenset({"offline", "offline_said", "offline_probe", "disk", "disk_val", "inode", "inode_val"})


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def start(s) -> datetime | None:
    """С чего считается подключение: с первого отчета агента. Агента поставили через неделю
    после заведения сервера - отсчет с его первого отчета, а не с заведения."""
    return _aware(getattr(s, "first_report_at", None) or getattr(s, "created_at", None))


def until(s) -> datetime | None:
    st = start(s)
    return st + ONBOARD_MAX if st else None


def active(s, now: datetime) -> bool:
    """Идет подключение: сервер заведен в панели (метка "onboard" в состоянии алертов, ее ставит
    create_server), его еще не закрыли, и с первого отчета не прошло ONBOARD_MAX."""
    if getattr(s, "onboard_done_at", None) is not None or not (getattr(s, "alert_state", None) or {}).get("onboard"):
        return False
    st = start(s)
    return st is not None and now - st < ONBOARD_MAX


def cond_ok(key: str, level: int) -> bool:
    """Пороговое условие уходит алертом и во время подключения: недоступен и критичный диск."""
    return key == "offline" or (key in ("disk", "inode") and level >= 3)


def msg_ok(kind: str) -> bool:
    return kind in _SEND


def state_kept(key: str) -> bool:
    return key in _SEND_STATE


def known_entry(by: str, now: datetime, level: int, sig=None) -> dict:
    """Известная проблема из того, что было при подключении. Уровень и что именно сломано пишем
    сразу: снимок на следующем тике принял бы за "как было" и то, что успело стать хуже."""
    return {"text": KNOWN_TEXT, "by": by or "авто", "at": now.isoformat(), "until": None, "onboard": True,
            "level": int(level), "sig": sorted(map(str, sig)) if sig else None}


def split(sid: int, msgs: list, states: list, now_apply: list) -> tuple[list, list]:
    """Сообщения и отметки одного сервера на подключении: что отправить и что записать сразу.
    Отметки придержанных событий пишутся сразу, как будто сообщение дошло: после закрытия
    подключения старое не придет."""
    keep = [m for m in msgs if msg_ok(getattr(m, "kind", ""))]
    kept_states = []
    for e in states:
        if keep and state_kept(e[1]):
            kept_states.append(e)
        else:
            now_apply.append(e)
    return keep, kept_states


def destinations(cfg: dict, group: str) -> dict:
    """Куда пойдут алерты сервера этой группы: основной чат и чаты по группам (имена, без id)."""
    if not cfg.get("telegram_token"):
        return {"tg": False, "main": False, "chats": []}
    routes = cfg.get("routes") or {}
    hits = [r for r in routes.get("chats") or [] if group and group in r.get("server_groups", [])]
    main = bool(cfg.get("telegram_chat")) and (routes.get("main") != "rest" or not hits)
    return {"tg": True, "main": main, "chats": [r.get("name") or "" for r in hits]}
