"""Фоновый планировщик мониторов: гоняет «созревшие» проверки, пишет тайм-серии,
ведёт инциденты (up→down переходы) и шлёт пороговые алерты.

Тик каждые settings.scheduler_tick секунд. Прунит старые снимки по retention.
"""

import asyncio
import html
import json
import logging
import random
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import NamedTuple
from urllib.parse import urlparse

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import alerts, audit, backup, backup_growth, checks as checks_exec, custom_backups, docker_exposure, flaky, heartbeat, settings_store
from app import backup_verify
from app import request_watch
from app import panel_errors
from app import inside_probe
from app import job_logs
from app import retention as retention_mod
from app import db_health
from app import access_audit
from app import docker_risk
from app import gpu as gpu_mod
from app import queue_watch
from app import workers as workers_mod
from app import cron_watch
from app import log_errors
from app import fpm_pools
from app import web_upstream
from app import listen_watch
from app import podnames
from app import mem_trend
from app import restarts as srv_restarts
from app import ssh_audit
from app import known as known_mod
from app import onboard as onboard_mod
from app import liveness
from app import disk_wear
from app import software as software_mod
from app import parents as parents_mod
from app import maint
from app import tg_bot
from app import web_certs
from app import web_config
from app import changes as srv_changes
from app import timeline as timeline_mod
from app import open_ports
from app import vulns as vulns_mod
from app import netlinks
from app import traffic as traffic_mod
from app import usage as usage_mod
from app import disk_forecast as dfc
from app import tg_buttons
from app.setup_scripts import current_setup_versions, gaps
from app.config import Settings, get_settings
from app.models import (
    AgentPathProbe,
    AuditLog,
    MaintenanceWindow,
    AgentProbe,
    Check,
    CheckIncident,
    CheckIpSample,
    CheckSample,
    BackupCommand,
    DockerCommand,
    DomainProbe,
    KubeCommand,
    Location,
    LocationResult,
    LocationSample,
    OomEvent,
    AlertEvent,
    LinkSample,
    ProbeRequest,
    RequestStat,
    Server,
    ServerEvent,
    ServerMetric,
    ServerTraffic,
    ServerUsage,
    WebErrorSample,
)

log = logging.getLogger("kervax.collector")

class Pending(NamedTuple):
    """Элемент очереди сайтовых алертов.

    Именно NamedTuple, а не голый кортеж: раньше длина была частью контракта, и
    добавление поля разом ломало каждое место распаковки. Новое поле с дефолтом
    в конце безопасно — старые семиэлементные вызовы продолжают работать."""

    kind: str
    name: str
    status: str
    msg: str
    inc_id: int | None
    check_id: int | None
    # None (инцидент) либо (атрибут, значение) — что выставить на мониторе при успехе
    flag: tuple[str, object] | None
    icon: str = ""  # готовая ведущая иконка (сроки: 🔥 истекло / ⚠️ предупреждение)


def _aware(dt: datetime) -> datetime:
    """SQLite отдаёт naive datetime; трактуем как UTC для сравнения с now."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_iso(v: object) -> datetime | None:
    """ISO-строка → aware datetime (или None). Для точечных снузов алертов."""
    if not isinstance(v, str):
        return None
    try:
        return _aware(datetime.fromisoformat(v))
    except ValueError:
        return None


# Момент старта процесса. Пока панель лежала (деплой, рестарт контейнера), агенты
# получали 502 и last_seen не обновлялся — после подъёма ВСЕ ноды выглядят молчащими.
# Слать за это «недоступен» нельзя: сервера-то работали, лежала панель.
_PANEL_STARTED = datetime.now(timezone.utc)
# сколько после старта не судить об оффлайне: агент шлёт раз в ~15с, берём с запасом
_START_GRACE_SECONDS = 180


def panel_just_started(now: datetime) -> bool:
    return (now - _PANEL_STARTED).total_seconds() < _START_GRACE_SECONDS


def seen_online(s: Server, now: datetime) -> bool:
    """Сервер на связи: агент присылал метрики не позже offline-порога назад."""
    return s.last_seen is not None and (now - _aware(s.last_seen)).total_seconds() <= max(
        s.offline_after_seconds, 30
    )


def effective_locations(check: Check, enabled: list[Location]) -> list[Location]:
    """Локации, из которых проверять этот монитор: None = все включённые (дефолт),
    [] = ни одной, [id,…] = выбранное подмножество (в порядке enabled)."""
    ids = check.location_ids
    if ids is None:
        return list(enabled)
    idset = set(ids)
    return [loc for loc in enabled if loc.id in idset]


# Сколько мониторов должно проверяться через локацию, чтобы "недоступны все" значило "сломан
# прокси": при одном-двух сайтах это с той же вероятностью блокировка их обоих в той стране
_BROKEN_MIN_CHECKS = 3


def broken_location_ids(
    probing: list[Location], checks: list[Check], results: dict, min_fails: int = 1
) -> set[int]:
    """Локации, у которых, похоже, сломан сам прокси: через нее проверяется не меньше трех
    мониторов, все они отсюда недоступны (каждый - не меньше min_fails раз подряд), а с самой
    панели хоть один отвечает. Тогда "недоступен из ..." по каждому сайту - шум, а не беда сайтов."""
    out: set[int] = set()
    for loc in probing:
        if not loc.url:
            continue
        got = [
            (c, r)
            for c in checks
            if c.enabled and c.check_locations and c.type == "http"
            and loc in effective_locations(c, probing)
            and (r := results.get((c.id, loc.id))) is not None
        ]
        if (
            len(got) >= _BROKEN_MIN_CHECKS
            and all(r.status == "down" and (r.consecutive_fails or 0) >= min_fails for _c, r in got)
            and any(c.last_status in ("up", "degraded") for c, _r in got)
        ):
            out.add(loc.id)
    return out


async def _gather_capped(coros, limit: int, jitter_s: float):
    """asyncio.gather с потолком одновременности и лёгким джиттером старта — чтобы
    исходящие проверки не летели все разом (снижает пик трафика/нагрузки). Порядок
    результатов сохраняется; исключения возвращаются как значения (не роняют пачку).
    limit<=0 и jitter<=0 → обычный gather."""
    if limit <= 0 and jitter_s <= 0:
        return await asyncio.gather(*coros, return_exceptions=True)
    sem = asyncio.Semaphore(limit) if limit > 0 else None

    async def run(coro):
        if jitter_s > 0:  # разброс старта — до захвата слота, чтобы не занимать его сном
            await asyncio.sleep(random.uniform(0, jitter_s))
        if sem is not None:
            async with sem:
                return await coro
        return await coro

    # return_exceptions=True: сбой одной проверки не роняет пачку (как раньше)
    return await asyncio.gather(*[run(c) for c in coros], return_exceptions=True)


def _is_due(check: Check, now: datetime) -> bool:
    if check.last_checked_at is None:
        return True
    return (now - check.last_checked_at).total_seconds() >= max(check.interval_seconds, 5)


def _needs_expiry(check: Check, now: datetime, settings: Settings) -> bool:
    """Пора ли обновить «медленные» сроки (TLS/домен) http-монитора."""
    if check.type != "http" or not (check.check_ssl or check.check_domain):
        return False
    if check.expiry_checked_at is None:
        return True
    return (now - check.expiry_checked_at).total_seconds() >= max(
        settings.expiry_refresh_hours * 3600, 60
    )


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def _display_host(target: str, ctype: str, port: int) -> str:
    """Короткий адрес монитора для заголовка алерта: http → host[/path] БЕЗ схемы,
    БЕЗ query-строки (там бывают токены/пароли — их нельзя светить в алерте!) и без
    user:pass@; tcp_port → host:port; cert → host."""
    t = (target or "").strip()
    if ctype == "http":
        parsed = urlparse(t if _SCHEME_RE.match(t) else "//" + t)
        host = parsed.hostname or _SCHEME_RE.sub("", t).split("/")[0].split("?")[0]
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return (host + parsed.path).rstrip("/")  # query отброшен
    if ctype == "tcp_port":
        return f"{t}:{port}" if port else t
    return t


_ALERT_ICON = {
    "recovery": "✅", "locrec": "✅", "ssl": "🔐", "domain": "🌐", "locpart": "🌍",
    "flaky": "🟠", "flakyrec": "✅",
}


def _expiry_icon(kind: str, days: int) -> str:
    """Ведущая иконка срока: 🔥 — уже истекло (авария), ⚠️ — пока предупреждение.

    Замок и глобус говорят, ЧТО истекает, но не насколько всё плохо: «остались
    сутки» и «просрочено» выглядели в ленте одинаково. Ноль дней — ещё
    предупреждение: до самой даты может оставаться почти сутки (см. _expiry_text)."""
    return ("🔥" if days < 0 else "⚠️") + _ALERT_ICON.get(kind, "")

# Иконка серверного алерта по типу (текст правил — только «суть», без иконки).
# «Недоступен» — 🔥: в ленте это самое тяжёлое событие, и его надо узнавать не читая.
# Монитор 🖥 стоял и у него, и у порогов CPU/RAM — все строки выглядели одинаково.
_SRV_ICON = {
    # Пороговые метрики ведём знаком предупреждения: в ленте они должны читаться
    # как «что-то не так», а не сливаться с информационными строками. У диска
    # иконка ещё и уточняется по серьёзности — см. _srv_icon_for.
    "offline": "🔥", "cpu": "⚠️", "mem": "⚠️", "disk": "⚠️", "temp": "🌡",
    "throttle": "🥵", "conntrack": "🔗", "disktemp": "🌡", "reboot": "🔄", "oom": "🧠",
    "db_conn": "🔌",
    # огонёк перед китом: в ленте докерные строки шли теми же иконками, что и
    # обычные события, и «упал»/«крутится в цикле» не читались как авария
    "docker_down": "🔥🐳", "docker_loop": "🔥🐳",
    "queue": "🐇", "backup_rotation": "🧹", "backup_lock": "🔒",
    "backup_unmonitored": "💾", "backup_check": "🩺", "backup_prune": "🧹",
    "backup_missing": "💾", "backup_failed": "💾", "backup_stale": "💾", "backup_repo": "💾",
    "backup_dump": "💾", "backup_dump_space": "🈵", "backup_cron": "💾", "backup_custom": "💾",
    "clock": "🕐", "dns": "🌐", "disk_health": "💽", "inode": "⚠️", "disk_forecast": "📈", "units": "⚙️",
    "unit_loop": "🔁⚙️",
    "cpu_spin": "🌀", "backup_growth": "💾📈", "docker_sock": "🔓🐳", "docker_risk": "🔑🐳",
    # ⏳ — срок ещё не вышел, есть время спланировать; 🔥 — доставка уже встала
    "kube_expiry": "⏳", "flux_down": "🔥☸️", "kube_pod": "☸️🔥", "web_5xx": "🌐🔥",
    # репозиторий чартов недоступен, а развернутое работает: предупреждение, а не авария
    "flux_stale": "⚠️☸️", "kube_cron": "⏰☸️",
    "exposed": "🔓", "port_new": "🚪", "pressure": "🫁", "net_health": "📶", "link": "🔗", "traffic": "📊",
    "kube_node": "🖥☸️", "kube_job": "💥☸️", "access_change": "🔑",
    "backup_restore": "🛟💾", "docker_restarts": "🔄🐳", "kube_restarts": "🔄☸️",
    "mem_leak": "📈🧠", "ssh_risk": "🔐", "access_risk": "🗝", "db_health": "🛢",
    "web_config": "🧾", "web_cert": "🔏", "listen_gone": "🔌", "disk_slow": "🐢",
    "gpu": "🎛", "gpu_heat": "🌡", "llm": "💬", "queue_stuck": "⏳", "queue_failed": "🧨", "workers": "👷",
    "cron_stuck": "⏲", "log_errors": "📜", "fpm": "🐘",
}

# Куда ведёт ссылка алерта (deep-link ?server=id&sec=…). Целимся в КОНКРЕТНУЮ метрику,
# а не в раздел: фронт сперва ищет карточку mcard-<sec> и только потом раздел msec-<sec>.
# Раньше ссылка на переполненный диск открывала раздел «Диск» с его начала — а там
# графики ввода-вывода, тогда как заполнение лежит в самом низу.
# offline/reboot — без цели (верх страницы): там смотреть нечего.
_SRV_SECTION = {
    "cpu": "cpu", "throttle": "throttle", "temp": "temp",
    "mem": "mem", "oom": "oom",
    "conntrack": "conntrack", "disk": "diskfill", "disktemp": "disktemp",
    "kube_expiry": "kube", "flux_down": "kube", "flux_stale": "kube", "kube_pod": "kube", "web_5xx": "web",
    "clock": "clock", "disk_health": "diskhealth", "inode": "diskfill", "disk_forecast": "diskfill", "units": "units",
    "unit_loop": "unitloop",
    "cpu_spin": "cpueat",
    "exposed": "exposure", "port_new": "exposure", "pressure": "pressure", "net_health": "nethealth",
    "link": "links", "traffic": "traffic",
    "access_change": "changes", "mem_leak": "memleak", "ssh_risk": "ssh", "access_risk": "access",
    "listen_gone": "exposure", "disk_slow": "disklat", "gpu": "gpu", "gpu_heat": "gpu",
}


# Диск шлёт три уровня, и ведущая иконка должна их различать: предупреждение,
# проблема, критично. Для остальных типов берём иконку из _SRV_ICON как есть.
_DISK_ICON = {1: "⚠️", 2: "🔴", 3: "🚨"}


# Раздел панели, к которому относится алерт — по нему персональная рассылка
# понимает, кого он касается (у учётки может не быть, скажем, «Бэкапов»).
# Вкладка карточки кластера в разделе "Кубер", куда ведет ссылка алерта.
# раздел карточки Docker-ноды по алерту: контейнеры или ключи от хоста
_DOCKER_SEC = {"docker_loop": "containers", "docker_down": "containers", "docker_restarts": "containers",
               "docker_sock": "host", "docker_risk": "host"}
# раздел окна бэкапа ноды по алерту
_BACKUP_SEC = {"backup_failed": "runs", "backup_stale": "runs", "backup_growth": "runs", "backup_dump": "coverage",
               "backup_dump_space": "coverage", "backup_cron": "coverage", "backup_restore": "restore",
               "backup_custom": "own"}
# раздел окна сервера бэкапов по алерту и по проблеме
_BSRV_SEC = {"backup_repo": "repos", "backup_lock": "repos", "backup_unmonitored": "repos", "backup_check": "repos",
             "backup_prune": "cleanup", "backup_rotation": "cleanup"}
# раздел карточки сервисов ноды по алерту и по проблеме
_SVC_SEC = {"queue": "queues", "queue_stuck": "queues", "queue_failed": "queues", "workers": "workers", "cron_stuck": "cron", "log_errors": "logs", "fpm": "fpm", "db_conn": "db", "db_health": "db", "web_config": "web", "web_cert": "certs",
            "llm": "llm"}
_KUBE_TAB = {"kube_expiry": "expiry", "flux_down": "flux", "flux_stale": "flux", "kube_pod": "pods",
             "kube_cron": "cron", "kube_node": "nodes", "kube_job": "cron", "kube_restarts": "pods"}

_ALERT_SECTION = {
    "docker_loop": "docker",
    "docker_down": "docker",
    "docker_sock": "docker",
    "docker_risk": "docker",
    "queue": "services",
    "queue_stuck": "services",
    "queue_failed": "services",
    "workers": "services",
    "cron_stuck": "services",
    "log_errors": "services",
    "fpm": "services",
    "backup_repo": "backups",
    "backup_rotation": "backups",
    "backup_lock": "backups",
    "backup_unmonitored": "backups",
    "backup_check": "backups",
    "backup_prune": "backups",
    "kube_pod": "kuber",
    "kube_cron": "kuber",
    "kube_node": "kuber",
    "kube_job": "kuber",
    "backup_restore": "backups",
    "docker_restarts": "docker",
    "kube_restarts": "kuber",
    "db_health": "services",
    "web_config": "services",
    "web_cert": "services",
    "llm": "services",
}


def _server_alert_text(
    kind: str, name: str, detail: str, link_url: str = "",
    recovery: bool = False, icon: str = "", group: str = "", ref: str = "",
) -> alerts.Msg:
    """Дефолтный формат серверного алерта — как у сайтов, без «Kervax:»:
    «<иконка> <сервер> — <detail>», где имя сервера — ссылка на монитор в панели
    (без кавычек — ссылка и так выделена). Весь динамический контент экранируется.

    group — группа сервера: нужна персональной доставке, чтобы не слать алерт про
    чужую инфраструктуру тому, кто её и в панели не видит."""
    icon = "✅" if recovery else (icon or _SRV_ICON.get(kind) or "🖥")
    nm = html.escape(name)
    linked = f'<a href="{html.escape(link_url, quote=True)}">{nm}</a>' if link_url else nm
    d = html.escape(detail)
    return alerts.Msg(
        f"{icon} {linked}" + (f" — {d}" if d else ""),
        _ALERT_SECTION.get(kind, "servers"),
        group, kind=kind, target=name, recovery=recovery, ref=ref,
    )


def _site_url(target: str, ctype: str) -> str:
    """URL для ссылки «открыть сам сайт» из алерта: scheme://host/path БЕЗ query
    (там бывают токены/пароли — не тащим их в ссылку) и без user:pass@. Только для
    http-мониторов; для tcp/cert — пусто (нечего открывать в браузере)."""
    if ctype != "http":
        return ""
    t = (target or "").strip()
    parsed = urlparse(t if _SCHEME_RE.match(t) else "https://" + t)
    host = parsed.hostname or ""
    if not host:
        return ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme or 'https'}://{host}{parsed.path}"


_MENTION_RE = re.compile(r"@[A-Za-z0-9_]+")


def _alert_text(
    kind: str, name: str, status: str, msg: str,
    host: str = "", link_url: str = "", site_url: str = "", icon: str = "",
) -> str:
    """Дефолтный формат сайтового алерта — коротко и по делу: «<иконка> <адрес> —
    <текст> · монитор». Две ссылки: адрес → открыть сам сайт (site_url без query,
    токены не светятся), «монитор» → открыть монитор в панели (link_url). Имя монитора
    как таковое НЕ показываем (не нужно), но @упоминания из него добавляем в конец —
    чтобы Telegram тегал людей. Весь динамический контент экранируется."""
    icon = icon or _ALERT_ICON.get(kind) or ("🔴" if status == "down" else "🟡")
    h = html.escape(host)
    m = html.escape(msg)
    detail = html.escape("снова доступен из всех локаций") if kind == "locrec" else m

    def a(href: str, inner_html: str) -> str:
        return f'<a href="{html.escape(href, quote=True)}">{inner_html}</a>' if href else inner_html

    addr = a(site_url, h) if h else ""  # адрес — ссылка на сам сайт
    lead = f"{icon} {addr}" if addr else icon
    tail = f" · {a(link_url, 'монитор')}" if link_url else ""  # ссылка на монитор в панели
    # @упоминания из имени монитора — для тегов в Telegram (само имя не показываем)
    ments = " ".join(_MENTION_RE.findall(name or ""))
    ment = f" · {html.escape(ments)}" if ments else ""

    if kind == "locpart":  # msg — многострочный список локаций (уже с 🟢/🔴)
        return f"{lead}{tail}{ment}\n{m}"
    return lead + (f" — {detail}" if detail else "") + tail + ment


# «уже истекло» — виртуальный порог теснее любого настроенного. Без него самый
# последний сигнал приходился на «остался 1 день», а факт истечения проходил молча:
# следующий bucket не становился теснее, и напоминание не отправлялось.
_EXPIRED_BUCKET = -1


def _expiry_bucket(days: int, thresholds: list[int], alerted: int | None) -> int | None:
    """Порог для НОВОГО напоминания или None. Алертим при входе в очередной (более
    тесный) порог — по одному на порог. thresholds — дни, напр. [14,7,1]."""
    crossed = [thr for thr in thresholds if days <= thr]
    if days < 0:
        crossed.append(_EXPIRED_BUCKET)
    if not crossed:
        return None
    bucket = min(crossed)  # самый тесный достигнутый порог
    return bucket if alerted is None or bucket < alerted else None


def _expiry_text(what: str, days: int, expired: str) -> str:
    """Текст напоминания. Ноль дней — это «истекает сегодня»: до самой даты может
    оставаться почти сутки, называть такое «истёк» — враньё и лишняя паника."""
    if days > 0:
        return f"{what} истекает через {days} дн."
    if days == 0:
        return f"{what} истекает сегодня"
    return expired


class DomainAlerts:
    """Срок регистрации - один на домен, а мониторов на его поддоменах бывает десяток: на одном
    домене их 10, и напоминание пришло бы десять раз. Шлем одно на домен и порог, остальным
    мониторам порог отмечаем молча. Состояние - из всех мониторов на начало прохода."""

    def __init__(self, rows) -> None:
        # домен -> {id монитора: [alerted_days, domain_days]}
        self.by: dict[str, dict[int, list]] = {}
        for cid, target, name, alerted, days in rows:
            self.by.setdefault(checks_exec._registrable_domain(target or name or ""), {})[cid] = [alerted, days]

    def count(self, dom: str) -> int:
        return len(self.by.get(dom) or {})

    def should_alert(self, dom: str, cid: int, bucket: int) -> bool:
        """Порог по домену еще не отмечен ни у одного другого монитора."""
        others = [a for c, (a, _) in (self.by.get(dom) or {}).items() if c != cid and a is not None]
        self.by.setdefault(dom, {}).setdefault(cid, [None, None])[0] = bucket
        return not any(a <= bucket for a in others)

    def first_renewed(self, dom: str, cid: int, days: int, limit: int) -> bool:
        """Продление еще не объявлено: ни у одного другого монитора домена эскалация не сброшена
        со свежим сроком за порогами."""
        others = [(a, d) for c, (a, d) in (self.by.get(dom) or {}).items() if c != cid]
        self.by.setdefault(dom, {})[cid] = [None, days]
        return not any(a is None and d is not None and d > limit for a, d in others)


def _apply_expiry(row: Check, info, now: datetime, pending: list, doms: DomainAlerts | None = None) -> None:
    """Сохраняет сроки TLS/домена и ставит эскалационные напоминания (по одному
    на каждый порог из ssl_warn_days/domain_warn_days). Невалидный/истёкший TLS
    на https уже даёт down основной проверки — здесь только про сроки. Срок домена -
    одним сообщением на домен (doms)."""
    row.expiry_checked_at = now
    # проба вернула дни → сохраняем; вернула None (таймаут/сбой) → НЕ затираем ранее
    # полученную валидную дату транзиентной ошибкой (иначе «домен: ConnectTimeout»).
    if info.ssl_days is not None:
        row.ssl_days = info.ssl_days
        row.ssl_message = info.ssl_message[:256]
    elif row.ssl_days is None:
        row.ssl_message = info.ssl_message[:256]
    if info.domain_days is not None:
        row.domain_days = info.domain_days
        row.domain_message = info.domain_message[:256]
    elif row.domain_days is None:
        row.domain_message = info.domain_message[:256]

    if row.check_ssl and info.ssl_days is not None:
        thr = row.ssl_warn_days or []
        if thr and info.ssl_days > max(thr):
            # Перевыпустили → сбрасываем эскалацию И сообщаем об этом. Раньше тут
            # была тишина: человек продлевал по нашему же алерту и не понимал,
            # увидела ли это панель. Закрытие темы стоит одного сообщения.
            if row.ssl_alerted_days is not None:
                pending.append((
                    "ssl", row.name, "",
                    f"SSL-сертификат перевыпущен, до истечения {info.ssl_days} дн.",
                    None, row.id, None, "✅",
                ))
            row.ssl_alerted_days = None
        bucket = _expiry_bucket(info.ssl_days, thr, row.ssl_alerted_days)
        if bucket is not None:
            txt = _expiry_text("SSL-сертификат", info.ssl_days, "SSL-сертификат истёк")
            pending.append(
                ("ssl", row.name, "", txt, None, row.id, ("ssl_alerted_days", bucket),
                 _expiry_icon("ssl", info.ssl_days))
            )

    if row.check_domain and info.domain_days is not None:
        thr = row.domain_warn_days or []
        dom = checks_exec._registrable_domain(row.target or row.name or "")
        n = doms.count(dom) if doms else 1
        more = f" (мониторов на нем: {n})" if n > 1 else ""
        if thr and info.domain_days > max(thr):
            if row.domain_alerted_days is not None and (
                    doms is None or doms.first_renewed(dom, row.id, info.domain_days, max(thr))):
                pending.append((
                    "domain", row.name, "",
                    f"регистрация домена {dom} продлена, до истечения {info.domain_days} дн.",
                    None, row.id, None, "✅",
                ))
            elif doms is not None:
                doms.first_renewed(dom, row.id, info.domain_days, max(thr))
            row.domain_alerted_days = None
        bucket = _expiry_bucket(info.domain_days, thr, row.domain_alerted_days)
        if bucket is not None:
            if doms is None or doms.should_alert(dom, row.id, bucket):
                txt = _expiry_text(
                    f"регистрация домена {dom}", info.domain_days, f"регистрация домена {dom} истекла"
                ) + more
                pending.append(
                    ("domain", row.name, "", txt, None, row.id, ("domain_alerted_days", bucket),
                     _expiry_icon("domain", info.domain_days))
                )
            else:
                row.domain_alerted_days = bucket  # о домене уже сказано по другому монитору


def _host_of_target(target: str) -> str:
    """Хост из адреса монитора: «https://gr.example.ru/health» → «gr.example.ru»."""
    d = (target or "").strip().lower().rstrip(".")
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/")[0]
    return d.split(":", 1)[0]


# Сколько домен может отсутствовать в списке ноды, прежде чем монитор от неё отвяжут.
# Список собирается раз в 15 минут, и один неполный сбор — деплой, пересоздание
# контейнера, упавший helper — не повод считать, что сайт с ноды ушёл: два полных цикла.
# Переезд на ДРУГУЮ ноду при этом срабатывает сразу — там домен виден, ждать нечего.
_UNBIND_GRACE = 30 * 60
# check_id → когда домен последний раз был виден на привязанной ноде. В памяти
# планировщика: после его рестарта в худшем случае повторится ровно старое поведение.
_bound_seen: dict[int, datetime] = {}


async def rebind_local_probes(session_factory) -> int:
    """Привязывает мониторы с галочкой «локально» к ноде, обслуживающей их домен.

    Человек ставит галочку, а не выбирает сервер: панель и так знает, чьи веб-серверы
    держат этот домен (агенты присылают их в web_services). Пересчитываем каждый цикл,
    потому что сайт переезжает с ноды на ноду, а галочка остаётся — при жёсткой
    привязке монитор молча ушёл бы проверяться туда, где сайта уже нет.

    Ноду не находим — probe_server_id остаётся пустым, и монитор честно скажет, что
    проверять его некому: это лучше, чем зелёный статус ни от кого."""
    async with session_factory() as session:
        checks = list(
            await session.scalars(
                select(Check).where(Check.enabled.is_(True), Check.probe_local.is_(True))
            )
        )
        if not checks:
            return 0
        serving: dict[str, int] = {}
        for srv in sorted(await session.scalars(select(Server)), key=lambda x: x.name):
            for web in (srv.last_report or {}).get("web_services") or []:
                for raw in web.get("sites") or []:
                    host = _host_of_target(raw)
                    if host and host not in serving:
                        serving[host] = srv.id
        changed = 0
        now = datetime.now(timezone.utc)
        online = {
            srv.id for srv in await session.scalars(select(Server)) if seen_online(srv, now)
        }
        for c in checks:
            want = serving.get(_host_of_target(c.target))
            if want is not None and want == c.probe_server_id:
                _bound_seen[c.id] = now
            # Домен пропал из списка, а нода, которая его держала, на связи: скорее всего
            # это неполный сбор на деплое, а не переезд сайта. Держим привязку, пока не
            # выйдет _UNBIND_GRACE, — агент продолжает проверять, ложного алерта нет.
            if want is None and c.probe_server_id is not None and c.probe_server_id in online:
                seen = _bound_seen.setdefault(c.id, now)
                if (now - seen).total_seconds() < _UNBIND_GRACE:
                    continue
            # Отметку ставим и при первом взгляде на монитор, и при смене ноды: в
            # обоих случаях результата от агента ещё нет, и это не падение сайта.
            if c.probe_server_id != want or c.probe_bound_at is None:
                c.probe_server_id = want
                c.probe_bound_at = now
                changed += 1
                if want is not None:
                    _bound_seen[c.id] = now
        if changed:
            await session.commit()
        return changed


# Сколько интервалов монитора ждать первый результат от агента, прежде чем считать
# его отсутствие падением. Задание агент забирает своим отчётом, результат присылает
# следующим — то есть до двух его циклов; берём три интервала и не меньше трёх минут.
_PROBE_WARMUP_INTERVALS = 3


def _probe_warming_up(c: Check, now: datetime) -> bool:
    """Локальный монитор только что привязан к ноде — данных ещё быть не может."""
    since = c.probe_bound_at or c.created_at
    if since is None:
        return False
    limit = max(c.interval_seconds * _PROBE_WARMUP_INTERVALS, 180)
    return (now - _aware(since)).total_seconds() < limit


def _paths_warming_up(c: Check, now: datetime) -> bool:
    """The same for the additional paths: they were just added or the monitor just bound."""
    stamps = [_aware(x) for x in (c.paths_changed_at, c.probe_bound_at, c.created_at) if x is not None]
    if not stamps:
        return False
    limit = max(c.interval_seconds * _PROBE_WARMUP_INTERVALS, 180)
    return (now - max(stamps)).total_seconds() < limit


def _sample_phases(ph: dict | None) -> dict:
    """Фазы запроса в колонки снимка (CheckSample.ph_*): из них график времени ответа."""
    if not isinstance(ph, dict):
        return {}
    return {f"ph_{k}": ph[k] for k in ("dns", "connect", "tls", "wait") if isinstance(ph.get(k), int)}


async def record_outcome(
    session: AsyncSession,
    row: Check,
    outcome: "checks_exec.CheckOutcome",
    now: datetime,
    pending: list,
    *,
    manual: bool = False,
) -> None:
    """Записывает результат проверки: снимок, статус монитора, инцидент, алерты.

    Одна дорожка и для планировщика, и для кнопки «Проверить сейчас». Раньше ручная
    проверка писала снимок и статус, а инциденты не трогала вовсе: монитор зеленел,
    а в карточке рядом висело «идёт сейчас», на главной — красное «Проблемы: 0» и
    «1 откр. инцидентов». Человек не мог понять, работает сайт или нет.

    manual=True отличается в одном: неудача не двигает счётчик «N неудачных подряд»
    и сама алерт не шлёт. Порог задуман как «сбой держится ~N интервалов», а три
    нажатия за пять секунд — это не три минуты простоя, и дежурный чат не должен
    узнавать о сайте от того, кто на него сейчас и так смотрит. Успех же закрывает
    инцидент по-настоящему — с отбоем, если о падении уже написали."""
    msg = outcome.message[:512]
    session.add(
        CheckSample(
            check_id=row.id,
            status=outcome.status,
            latency_ms=outcome.latency_ms,
            value=outcome.value,
            message=msg,
            ts=now,
            **_sample_phases(outcome.phases),
        )
    )
    new_status = outcome.status
    if new_status == "up":
        row.consecutive_fails = 0
    elif not manual:
        row.consecutive_fails = (row.consecutive_fails or 0) + 1
    if new_status == "down" and not manual and row.consecutive_fails == 1:
        # первый сбой сайта из панели: спросить его сервер, открывается ли он изнутри. Порог
        # алерта обычно две неудачи, и к алерту ответ уже будет - строкой в том же сообщении
        try:
            await inside_probe.request(session, row, now)
        except Exception:
            log.warning("не попросил проверку изнутри для %s", row.name, exc_info=True)

    open_inc = await session.scalar(
        select(CheckIncident).where(
            CheckIncident.check_id == row.id,
            CheckIncident.ended_at.is_(None),
        )
    )
    if new_status != "up":
        if open_inc is None:
            open_inc = CheckIncident(
                check_id=row.id, status=new_status,
                started_at=now, last_message=msg, notified=False,
            )
            session.add(open_inc)
            await session.flush()  # получить id
        else:
            open_inc.status = new_status
            open_inc.last_message = msg
        # деградация («медленно») шумнее — свой, обычно больший порог
        threshold = max(
            row.degraded_after_failures if new_status == "degraded"
            else row.alert_after_failures,
            1,
        )
        if not manual and not open_inc.notified and row.consecutive_fails >= threshold:
            pending.append(
                ("bad", row.name, new_status, msg, open_inc.id, row.id, None, "")
            )
    elif open_inc is not None:
        open_inc.ended_at = now
        if open_inc.notified:
            # несём up-сообщение («HTTP 200 · N мс» / «порт открыт …») —
            # чтобы в восстановлении был виден код/латентность, а не пусто
            pending.append(("recovery", row.name, "up", msg, None, row.id, None, ""))
    if not manual:
        await _flaky_step(session, row, new_status, msg, now, pending)
    if row.known_issue and not manual:
        # известная проблема монитора: закрыть, когда он поднялся и держится, или по сроку
        new_k, what = known_mod.step_check(row.known_issue, new_status == "up" and row.flaky_since is None, now)
        if new_k != row.known_issue:
            row.known_issue = new_k
        if what in ("closed", "expired"):
            session.add(AuditLog(username="авто", action="known_" + what, target=row.name[:255], detail=""))
        if what == "remind" and new_k is not None:
            pending.append(Pending("known", row.name, new_status,
                                   known_mod.remind_text(msg or new_status, new_k, now), None, row.id, None, "📌"))

    if row.type == "dns" and outcome.answers is not None:
        # последний ответ - для кнопки "принять"; первый удачный без ожидаемого - запоминаем
        row.dns_last = outcome.answers
        if outcome.status == "up" and outcome.answers and not row.dns_baseline and not (row.dns_expect or "").strip():
            row.dns_baseline = outcome.answers

    row.last_status = new_status
    row.last_message = msg
    row.last_latency_ms = outcome.latency_ms
    row.last_value = outcome.value
    row.last_checked_at = now
    # Монитор ТИПА «сертификат» сам и есть проверка срока: дни приходят в
    # value. Отдельный проход по срокам (probe_expiry) ходит только к
    # http-мониторам, поэтому ssl_days у cert оставался пустым — а на нём
    # держится всё остальное: чип срока в списке, блок «истекает» на
    # главной, группировка по домену. Данные были, показать их было нечем.
    if row.type == "cert" and outcome.value is not None:
        row.ssl_days = int(outcome.value)
        row.expiry_checked_at = now
    # разбивка по IP (режим «все адреса») — снимок для детали + точки в
    # тайм-серию по каждому адресу (для графика времени ответа по IP)
    # split by additional paths: the card shows which path failed (None: the monitor has no paths)
    row.last_path_results = outcome.path_results
    row.last_phases = outcome.phases
    if outcome.ip_results is not None:
        row.last_ip_results = outcome.ip_results
        for ipr in outcome.ip_results:
            session.add(CheckIpSample(
                check_id=row.id, ip=ipr["ip"], status=ipr["status"],
                latency_ms=ipr.get("latency_ms"), ts=now,
            ))


async def _flaky_step(
    session: AsyncSession, row: Check, new_status: str, msg: str, now: datetime, pending: list
) -> None:
    """Сайт отвечает через раз (flaky.py): сбои вперемешку с успешными проверками, которых
    алерт "N неудач подряд" не видит. Ручная проверка сюда не попадает, как и в счетчик подряд.

    Состояние (flaky_since) меняем сразу, а алерт и отбой ставим в очередь, пока они не уйдут:
    flaky_notified выставит _send_alerts только после доставки."""
    thr = max(row.alert_after_failures, 1)
    prev = (
        await session.execute(
            select(CheckSample.status, CheckSample.message)
            # нынешний снимок уже в сессии и может попасть в выборку при автофлаше
            .where(CheckSample.check_id == row.id, CheckSample.ts < now)
            .order_by(CheckSample.ts.desc())
            .limit(flaky.WINDOW + thr)
        )
    ).all()
    hist = [(st, m) for st, m in reversed(prev)] + [(new_status, msg)]
    statuses = [st for st, _ in hist]
    was = row.flaky_since is not None
    now_flaky = flaky.is_flaky(statuses, thr, was)
    if now_flaky and not was:
        row.flaky_since = now
    elif was and not now_flaky:
        row.flaky_since = None
    if now_flaky and not row.flaky_notified:
        fails, _ = flaky.short_failures(statuses, thr)
        last_err = next((m for st, m in reversed(hist) if st == "down" and m), "")
        text = (
            f"отвечает через раз: {fails} из {min(len(statuses), flaky.WINDOW)} "
            f"последних проверок не прошли"
        )
        if last_err:
            text += f", последняя ошибка: {last_err}"
        pending.append(
            Pending("flaky", row.name, new_status, text, None, row.id, ("flaky_notified", True), "")
        )
    elif not now_flaky and row.flaky_notified:
        pending.append(
            Pending(
                "flakyrec", row.name, "up", "снова отвечает стабильно", None, row.id,
                ("flaky_notified", False), "",
            )
        )


async def send_alerts_soon(session_factory, pending: list, now: datetime) -> None:
    """Отправка алертов из веб-запроса (ручная проверка, отчёт агента): ошибка канала
    не должна превращаться в ошибку кнопки — результат проверки уже записан."""
    if not pending:
        return
    try:
        await _send_alerts(session_factory, get_settings(), pending, now)
    except Exception:  # noqa: BLE001
        log.exception("ошибка отправки алертов ручной проверки")


async def run_due_checks(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        enabled = list(
            await session.scalars(select(Check).where(Check.enabled.is_(True)))
        )
    due = [c for c in enabled if _is_due(c, now)]
    if not due:
        return 0

    cap = settings.check_max_concurrency
    jitter = max(settings.check_jitter_ms, 0) / 1000.0
    # Сайт за белым списком панель проверить не может — снаружи соединение просто
    # рвут. За неё это делает агент НА САМОМ СЕРВЕРЕ и присылает сырой результат;
    # здесь мы его только оцениваем — теми же порогами, что и всё остальное.
    local = [c for c in due if c.probe_local]
    remote = [c for c in due if not c.probe_local]
    outcomes_map: dict[int, object] = {}
    probes: dict[int, AgentProbe] = {}
    if remote:
        got = await _gather_capped([checks_exec.run_check(c) for c in remote], cap, jitter)
        outcomes_map.update(dict(zip((c.id for c in remote), got)))
    if local:
        async with session_factory() as session:
            probes = {
                r.check_id: r
                for r in await session.scalars(
                    select(AgentProbe).where(AgentProbe.check_id.in_([c.id for c in local]))
                )
            }
        # Молчание агента в первые минуты после привязки — это не «сайт лежит», а
        # «результат ещё в пути». Раньше монитор успевал покраснеть, открыть инцидент
        # и обнулить суточный аптайм, а через минуту зеленел сам: человек видел ровно
        # ту картину, ради которой мониторинг и заводят, и она была ложной.
        warming = {c.id for c in local if probes.get(c.id) is None and _probe_warming_up(c, now)}
        if warming:
            due = [c for c in due if c.id not in warming]
            local = [c for c in local if c.id not in warming]
        # additional paths come from the agent as separate results, one row per path
        path_rows: dict[int, dict[str, AgentPathProbe]] = {}
        with_paths = [c.id for c in local if checks_exec.extra_paths_of(c)]
        if with_paths:
            async with session_factory() as session:
                for r in await session.scalars(
                    select(AgentPathProbe).where(AgentPathProbe.check_id.in_(with_paths))
                ):
                    path_rows.setdefault(r.check_id, {})[r.path] = r
        for c in local:
            main = checks_exec.outcome_from_agent(c, probes.get(c.id), now, c.degraded_ms)
            rows = {p: r for p, r in path_rows.get(c.id, {}).items() if r.server_id == c.probe_server_id}
            outcomes_map[c.id] = checks_exec.outcome_with_agent_paths(
                c, main, rows, now, c.degraded_ms, _paths_warming_up(c, now)
            )
    outcomes = [outcomes_map.get(c.id) for c in due]
    # «медленные» сроки (TLS/домен) обновляем только у созревших для этого мониторов
    refresh = [c for c in due if _needs_expiry(c, now, settings)]
    # У сайта за белым списком своя проверка сертификата невозможна: панель до него
    # не дотягивается, и в карточке висело «сайт недоступен (таймаут)» при живом
    # сертификате. Агент видел его на том же соединении, которым проверял сайт, —
    # берём оттуда. Срок ДОМЕНА при этом считает панель как обычно: это запрос в
    # RDAP, а не к самому сайту, и он проходит.
    exp_local = [c for c in refresh if c.probe_local]
    exp_remote = [c for c in refresh if not c.probe_local]
    exp_results = await _gather_capped(
        [checks_exec.probe_expiry(c) for c in exp_remote], cap, jitter
    )
    exp_map: dict[int, checks_exec.ExpiryInfo] = {
        c.id: r
        for c, r in zip(exp_remote, exp_results)
        if isinstance(r, checks_exec.ExpiryInfo)
    }
    if exp_local:
        dom_results = await _gather_capped(
            [checks_exec.probe_expiry_domain_only(c) for c in exp_local], cap, jitter
        )
        for c, dom in zip(exp_local, dom_results):
            info = checks_exec.expiry_from_agent(c, probes.get(c.id), now)
            if isinstance(dom, checks_exec.ExpiryInfo):
                info.domain_days, info.domain_message = dom.domain_days, dom.domain_message
            exp_map[c.id] = info

    # (kind, name, status, message, incident_id, check_id, flag)
    #   kind: "bad" | "recovery" | "ssl" | "domain"
    pending: list[Pending] = []

    async with session_factory() as session:
        doms = None
        if exp_map:
            doms = DomainAlerts((await session.execute(
                select(Check.id, Check.target, Check.name, Check.domain_alerted_days, Check.domain_days)
                .where(Check.check_domain.is_(True)))).all())
        for check, res in zip(due, outcomes):
            outcome = (
                res
                if isinstance(res, checks_exec.CheckOutcome)
                else checks_exec.CheckOutcome("down", message=str(res)[:500])
            )
            row = await session.get(Check, check.id)
            if row is None:
                continue
            await record_outcome(session, row, outcome, now, pending)
            if check.id in exp_map:
                _apply_expiry(row, exp_map[check.id], now, pending, doms)
        await session.commit()

    if pending:
        try:
            await _send_alerts(session_factory, settings, pending, now)
        except Exception:  # noqa: BLE001 — алерты не должны ронять цикл
            log.exception("ошибка отправки алертов")
    return len(due)


# pending-kind → тип правила (recovery локаций считаем частью locpart)
_SITE_RULE_KIND = {
    "bad": "down", "recovery": "recovery", "ssl": "ssl",
    "domain": "domain", "locpart": "locpart", "locrec": "locpart",
    "flaky": "flaky", "flakyrec": "flaky",
}
# раздел карточки монитора по алерту: сроки - в "Сертификат и домен", частичная доступность - в
# "Локации"; лежит и через раз - на сводку, там строка что и где оборвалось
_SITE_SEC = {"ssl": "exp", "domain": "exp", "locpart": "loc", "locrec": "loc"}
# История алертов: отбой - под видом самой беды, чтобы фильтр показывал ее целиком
_SITE_HISTORY_KIND = {"bad": "down", "recovery": "down"}
_SITE_RECOVERY = frozenset({"recovery", "locrec", "flakyrec"})


def _site_scope_ok(rule: dict, check_id: int | None, group: str) -> bool:
    st = rule.get("scope_type", "all")
    if st == "all":
        return True
    scope = rule.get("scope") or []
    if st == "groups":
        return group in scope
    if st == "checks":
        return check_id in scope
    return True


def _fmt_site(template: str, name: str, group: str, status: str, msg: str) -> str:
    try:
        return template.format(name=name, group=group, message=msg, status=status)
    except (KeyError, IndexError, ValueError):
        return template


def _as_pending(item) -> Pending:
    """Элемент очереди → именованный кортеж (на вход бывает и голый tuple)."""
    return item if isinstance(item, Pending) else Pending(*item)


async def _with_before(session_factory, msg: "alerts.Msg", s, end: datetime,
                       exclude: frozenset = frozenset()) -> "alerts.Msg":
    """Дописывает к алерту, что менялось на сервере за час до сбоя (app/timeline.py)."""
    try:
        start = end - timeline_mod.BEFORE
        async with session_factory() as ses:
            evs = (await timeline_mod.events_between(ses, [s.id], start, end)).get(s.id, [])
        ch = s.changes.get("log") if isinstance(getattr(s, "changes", None), dict) else []
        tail = timeline_mod.before_text(evs, ch or [], start, end, exclude)
    except Exception:
        log.warning("не собрал, что менялось перед сбоем на %s", getattr(s, "name", "?"), exc_info=True)
        return msg
    if not tail:
        return msg
    return alerts.Msg(msg + "\n" + html.escape(tail), msg.section, msg.group, msg.kind, msg.target,
                      msg.recovery, msg.ref)


async def _send_alerts(
    session_factory, settings: Settings, pending, now: datetime
) -> None:
    async with session_factory() as session:
        cfg = await settings_store.get_alert_config(session, settings)
        muted = await settings_store.get_muted(session)
        rules = await settings_store.get_site_alert_rules(session)
        # по ИМЕНИ поля, а не позиционной распаковкой с конца: раньше здесь было
        # (*_, cid, _f) — добавление восьмого элемента (иконки) сдвинуло разбор,
        # и вместо check_id бралcя flag: алерты теряли адрес сайта, ссылку на
        # монитор и проверку снуза/мьютов
        ids = [p.check_id for p in map(_as_pending, pending) if p.check_id is not None]
        groups: dict[int, str] = {}
        mutes: dict[int, set] = {}  # заглушённые типы алертов по монитору
        meta: dict[int, tuple[str, str, int]] = {}  # id → (target, type, port) для хоста
        snoozed: set[int] = set()  # мониторы с активным снузом — алерты не шлём
        known_ids: set[int] = set()  # мониторы с известной проблемой - молчим про сбой
        # окна обслуживания: монитор в идущем окне молчит, как под снузом
        _ms, maint_chk = maint.active(list(await session.scalars(select(MaintenanceWindow))), now,
                                      cfg.get("tz") or "Europe/Moscow")
        snoozed.update(i for i in ids if i in maint_chk)
        # Зависимости (app/parents.py): родитель монитора молчит - сбой придерживаем, он строкой в
        # сообщении про сервер; родитель на обслуживании - молчим, как в самом окне. Не помечаем:
        # родитель вернулся, а монитор все еще лежит - алерт придет следующим тиком.
        held: set[int] = set()
        fail_ids = {p.check_id for p in map(_as_pending, pending)
                    if p.check_id is not None and p.kind in ("bad", "flaky", "locpart")}
        # сайт лег - что менялось за час до этого на сервере, от которого он зависит
        down_ids = {p.check_id for p in map(_as_pending, pending) if p.check_id is not None and p.kind == "bad"}
        before_by: dict[int, str] = {}
        # сайт лег снаружи - открывается ли он изнутри своего сервера (app/inside_probe.py)
        inside_by: dict[int, str] = {}
        if ids:
            rows = await session.execute(
                select(
                    Check.id, Check.group_name, Check.alert_mutes,
                    Check.target, Check.type, Check.port, Check.snooze_until, Check.known_issue,
                    Check.probe_local, Check.probe_server_id, Check.parent_mode, Check.parent_server_id,
                    Check.last_inside,
                ).where(Check.id.in_(ids))
            )
            lite: list[SimpleNamespace] = []
            for i, g, m, tgt, typ, prt, snz, kn, pl, ps, pm, pp, li in rows.all():
                if i in down_ids and (line := inside_probe.alert_line(li, now)):
                    inside_by[i] = line
                groups[i] = g
                mutes[i] = set(m or [])
                meta[i] = (tgt, typ, prt)
                if snz is not None and _aware(snz) > now:
                    snoozed.add(i)
                if isinstance(kn, dict) and not known_mod.expired(kn, now):
                    known_ids.add(i)
                if i in fail_ids:
                    lite.append(SimpleNamespace(id=i, target=tgt, type=typ, probe_local=pl, probe_server_id=ps,
                                                parent_mode=pm, parent_server_id=pp))
            if lite:
                psrv = await parents_mod.load_servers(session)
                pidx = parents_mod.Index(psrv)
                silent = parents_mod.silent_ids(psrv, now, seen_online, panel_just_started(now))
                pids_by: dict[int, set[int]] = {}
                for c in lite:
                    pids = {pid for pid, _w in parents_mod.parents_of(c, pidx)}
                    if pids & silent:
                        held.add(c.id)
                    elif pids & _ms:
                        snoozed.add(c.id)
                    elif c.id in down_ids and pids:
                        pids_by[c.id] = pids
                if pids_by:
                    try:
                        all_p = set().union(*pids_by.values())
                        b_start = now - timeline_mod.BEFORE
                        evs = await timeline_mod.events_between(session, all_p, b_start, now)
                        chl = {sid: (ch or {}).get("log") or [] for sid, ch in (await session.execute(
                            select(Server.id, Server.changes).where(Server.id.in_(all_p)))).all()
                            if isinstance(ch, dict)}
                        for cid, pids in pids_by.items():
                            tail = timeline_mod.before_text(
                                [e for p_ in pids for e in evs.get(p_, [])],
                                [e for p_ in pids for e in chl.get(p_, [])], b_start, now)
                            if tail:
                                before_by[cid] = tail
                    except Exception:
                        log.warning("не собрал, что менялось перед падением сайтов", exc_info=True)
    if muted or not alerts.alerts_enabled(cfg):
        return
    base = settings.panel_url.rstrip("/")
    threshold = int(cfg.get("flood_threshold", 6))
    # Собираем сообщения тика + отложенные отметки, затем шлём с антифлудом.
    texts: list[alerts.Msg] = []
    commits: list[tuple] = []  # (kind, inc_id, check_id, flag)
    # Домен продлевают целиком, поэтому истекает ОДНО имя, а мониторов на его
    # поддоменах бывает много: без свёртки в чат прилетало пять одинаковых
    # «регистрация домена истекает через 4 дн.» про один и тот же example.com.
    # Флаги эскалации при этом ставим КАЖДОМУ монитору (иначе на следующем тике
    # остальные напишут по второму разу), а сообщение отправляем одно.
    dom_at: dict[str, int] = {}   # registrable-домен → индекс его текста в texts
    dom_n: dict[str, int] = {}    # сколько мониторов он покрывает
    # Сайт и его www (или два поддомена за одним фронтом) падают и поднимаются одним тиком и
    # одинаково: было по сообщению на монитор, с отбоями четыре вместо двух. Одинаковый вид и
    # статус на одном домене в одном тике - одно сообщение, остальные адреса дописаны.
    same_at: dict[tuple, int] = {}
    same_hosts: dict[tuple, list[str]] = {}
    # мониторы, свернутые в чужое сообщение: кнопки под ним глушат их все
    more_ids: dict[int, list[int]] = {}
    # Pending(*p): на вход может прийти и голый кортеж (в т.ч. из тестов) —
    # приводим к именованному, недостающий icon подставит дефолт
    for kind, name, status, msg, inc_id, check_id, flag, icon in map(_as_pending, pending):
        rk = _SITE_RULE_KIND.get(kind)
        rule = rules.get(rk) if rk else None
        grp = groups.get(check_id, "") if check_id is not None else ""
        if check_id is not None and check_id in snoozed:
            continue  # монитор временно приглушён (снуз) — не шлём, но и не помечаем
        if rk and check_id is not None and rk in mutes.get(check_id, set()):
            continue  # тип заглушён именно для этого монитора
        if kind == "known":
            # напоминание об известной проблеме монитора: без кнопок и без свертки по домену
            texts.append(alerts.Msg(f"{icon or '📌'} <b>{html.escape(name)}</b> — {html.escape(msg)}",
                                    "sites", grp or "", kind="known", target=name))
            continue
        if check_id is not None and check_id in known_ids and kind in ("bad", "flaky", "locpart"):
            continue  # известная проблема: про нее знают, молчим, пока не закроется или не выйдет срок
        if check_id is not None and check_id in held and kind in ("bad", "flaky", "locpart"):
            continue  # родитель молчит: лежит из-за него, это строка в сообщении про сервер
        tgt, typ, prt = meta.get(check_id, ("", "http", 0))
        # у push-монитора нет адреса: в заголовке алерта его имя
        host = name if typ == "push" else _display_host(tgt, typ, prt)
        site_url = _site_url(tgt, typ)  # ссылка «открыть сам сайт» (без query/токена)
        link_url = (f"{base}/?check={check_id}" + (f"&sec={_SITE_SEC[kind]}" if kind in _SITE_SEC else "")
                    if base and check_id is not None else "")
        zone = ""
        if kind == "domain":
            # в шапке показываем САМО истекающее имя, а не поддомен одного из
            # мониторов; ссылку на сайт убираем — она вела бы не туда, куда текст
            zone = checks_exec._registrable_domain(tgt or name)
            host, site_url = zone, ""
            if zone in dom_at:
                dom_n[zone] = dom_n.get(zone, 1) + 1
                if check_id is not None:
                    more_ids.setdefault(dom_at[zone], []).append(check_id)
                commits.append((kind, inc_id, check_id, flag))
                continue
        same = None
        # push и dns в один заголовок не сворачиваем: у каждого свой смысл, а не "тот же сайт"
        if kind in ("bad", "recovery") and tgt and typ not in ("push", "dns"):
            same = (kind, status, checks_exec._registrable_domain(tgt))
            if same in same_at:
                same_hosts[same].append(host or name)
                if check_id is not None:
                    more_ids.setdefault(same_at[same], []).append(check_id)
                commits.append((kind, inc_id, check_id, flag))
                continue
        if rule is not None:
            if not rule["enabled"] or not _site_scope_ok(rule, check_id, grp):
                continue  # тип выключен или монитор вне области применения
            default = settings_store.SITE_ALERT_KINDS[rk][1]
            is_default = (
                rule["text"] == default
                or rule["text"] in settings_store.LEGACY_SITE_DEFAULTS
            )
        else:
            is_default = True
        if is_default:
            # богатый формат: адрес(ссылка на сайт) + иконка + имя + текст + «монитор»(ссылка в панель)
            text = _alert_text(kind, name, status, msg, host, link_url, site_url, icon)
        else:
            # кастомный шаблон пользователя: рендерим и экранируем, ссылку — строкой
            text = html.escape(_fmt_site(rule["text"], name, grp, status, msg))
            if link_url:
                text += f"\n🔗 {html.escape(link_url)}"
        if zone:
            dom_at[zone] = len(texts)
            dom_n[zone] = 1
        if same is not None:
            same_at[same] = len(texts)
            same_hosts[same] = []
        if kind == "bad" and check_id in inside_by:
            text += "\n" + html.escape(inside_by[check_id])
        if kind == "bad" and check_id in before_by:
            text += "\n" + html.escape(before_by[check_id])
        # в истории отбой лежит под тем же видом, что и сама беда: фильтр по виду
        # показывает историю целиком
        texts.append(alerts.Msg(text, "sites", grp or "", kind=_SITE_HISTORY_KIND.get(kind, rk or kind),
                                target=name, recovery=kind in _SITE_RECOVERY,
                                ref=f"chk:{check_id}" if check_id is not None and rk else ""))
        commits.append((kind, inc_id, check_id, flag))

    # дописываем счётчик только там, где мониторов реально больше одного
    for zone, idx in dom_at.items():
        n = dom_n.get(zone, 1)
        if n > 1:
            old = texts[idx]
            texts[idx] = alerts.Msg(
                f"{old} · мониторов: {n}", old.section, old.group,
                kind=old.kind, target=old.target, recovery=old.recovery, ref=old.ref,
            )

    for key, idx in same_at.items():
        more = same_hosts.get(key) or []
        if more:
            old = texts[idx]
            texts[idx] = alerts.Msg(
                f"{old}\n↳ и {html.escape(', '.join(more))}", old.section, old.group,
                kind=old.kind, target=old.target, recovery=old.recovery, ref=old.ref,
            )

    for idx, ids in more_ids.items():
        old = texts[idx]
        if old.ref:
            texts[idx] = alerts.Msg(
                str(old), old.section, old.group, kind=old.kind, target=old.target,
                recovery=old.recovery, ref=old.ref + "".join(f",{i}" for i in ids),
            )

    # parse_mode=HTML: имя монитора идёт ссылкой <a href>. Весь контент экранирован.
    if not await alerts.dispatch(cfg, True, texts, threshold, parse_mode="HTML",
                                 session_factory=session_factory):
        return  # не доставлено — не помечаем, повторим на следующем тике
    async with session_factory() as session:
        for kind, inc_id, check_id, flag in commits:
            if kind == "bad" and inc_id is not None:
                inc = await session.get(CheckIncident, inc_id)
                if inc is not None:
                    inc.notified = True
            elif flag and check_id is not None:
                attr, value = flag  # напр. ("ssl_alerted_days", 7)
                row = await session.get(Check, check_id)
                if row is not None:
                    setattr(row, attr, value)
        await session.commit()


async def run_location_probes(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Гоняет http-мониторы (с check_locations) через каждую включённую прокси-локацию
    на своей каденции; хранит ПОСЛЕДНИЙ результат по паре (монитор, локация)."""
    now = datetime.now(timezone.utc)
    interval = max(settings.location_probe_interval, 30)
    async with session_factory() as session:
        enabled = list(
            await session.scalars(select(Location).where(Location.enabled.is_(True), Location.monitors.is_(True)))
        )
        checks = list(
            await session.scalars(
                select(Check).where(
                    Check.enabled.is_(True),
                    Check.check_locations.is_(True),
                    Check.type == "http",
                )
            )
        )
        existing = {
            (r.check_id, r.location_id): r
            for r in await session.scalars(select(LocationResult))
        }
    if not enabled or not checks:
        return 0

    jobs = [
        (c, loc)
        for c in checks
        for loc in effective_locations(c, enabled)
        if loc.url  # прямые локации (url="") не гоняем — это основная проверка
        and (
            (r := existing.get((c.id, loc.id))) is None
            or (now - _aware(r.checked_at)).total_seconds() >= interval
        )
    ]
    if not jobs:
        return 0

    outcomes = await _gather_capped(
        [checks_exec.probe_via_proxy(c, loc.url) for c, loc in jobs],
        settings.check_max_concurrency,
        max(settings.check_jitter_ms, 0) / 1000.0,
    )
    async with session_factory() as session:
        for (c, loc), res in zip(jobs, outcomes):
            outcome = (
                res
                if isinstance(res, checks_exec.CheckOutcome)
                else checks_exec.CheckOutcome("down", message=str(res)[:500])
            )
            row = await session.scalar(
                select(LocationResult).where(
                    LocationResult.check_id == c.id,
                    LocationResult.location_id == loc.id,
                )
            )
            if row is None:
                row = LocationResult(check_id=c.id, location_id=loc.id, consecutive_fails=0)
                session.add(row)
            row.status = outcome.status
            row.latency_ms = outcome.latency_ms
            row.message = outcome.message[:512]
            # у только что созданной строки consecutive_fails ещё None (default=0
            # применяется лишь при flush) → берём (… or 0), иначе None+1 роняет пачку
            row.consecutive_fails = (
                0 if outcome.status in ("up", "degraded")
                else (row.consecutive_fails or 0) + 1
            )
            row.checked_at = now
            # + точка в тайм-серию локации (для графика по локации)
            session.add(
                LocationSample(
                    check_id=c.id,
                    location_id=loc.id,
                    status=outcome.status,
                    latency_ms=outcome.latency_ms,
                    ts=now,
                )
            )
        await session.commit()
    return len(jobs)


async def _forget_stale_locations(session_factory, enabled: list[Location]) -> None:
    """Убирает вердикты локаций, которые монитору больше не назначены.

    Два случая, и оба живые: у монитора сняли галочку «проверять из локаций» —
    тогда прошлое «недоступен из Алматы» висело бы вечно; либо список локаций
    сузили, и результат по убранной точке застыл (видели запись с 4646 сбоями
    подряд по локации, через которую монитор давно не проверяется — включи её
    снова, и панель мгновенно сочла бы это аварией).

    Не выбираем по `loc_alerted IS NOT NULL`: JSON-колонка со значением None
    хранит JSON-null, для SQL это НЕ NULL, и такой запрос находил одни и те же
    строки на каждом тике — сброс крутился каждые 30 секунд впустую.
    """
    async with session_factory() as session:
        checks = list(await session.scalars(select(Check)))
        rows = list(await session.scalars(select(LocationResult)))
    have: dict[int, set[int]] = {}
    for r in rows:
        have.setdefault(r.check_id, set()).add(r.location_id)
    drop: list[tuple[int, int]] = []  # (check_id, location_id) — лишние результаты
    forget: list[int] = []            # мониторы, у которых пора снять отметку
    for c in checks:
        want = (
            {loc.id for loc in effective_locations(c, enabled)}
            if c.enabled and c.check_locations
            else set()
        )
        drop += [(c.id, lid) for lid in have.get(c.id, set()) - want]
        if c.loc_alerted and not (set(c.loc_alerted) & want):
            forget.append(c.id)
    if not drop and not forget:
        return
    async with session_factory() as session:
        for cid, lid in drop:
            await session.execute(
                delete(LocationResult).where(
                    LocationResult.check_id == cid, LocationResult.location_id == lid
                )
            )
        for cid in forget:
            row = await session.get(Check, cid)
            if row is not None:
                row.loc_alerted = None
        await session.commit()
    log.info(
        "локации: снято вердиктов %d, удалено устаревших результатов %d",
        len(forget), len(drop),
    )


async def evaluate_location_alerts(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> None:
    """Алерт о частичной доступности: если монитор доступен из одних локаций и
    НЕ доступен из других — шлём разбивку (откуда работает, откуда нет). При
    возврате к полной доступности — recovery. Полная недоступность отдельно не
    алертится тут (её покрывает основной инцидент)."""
    async with session_factory() as session:
        enabled = list(
            await session.scalars(select(Location).where(Location.enabled.is_(True), Location.monitors.is_(True)))
        )
        checks = list(
            await session.scalars(
                select(Check).where(
                    Check.enabled.is_(True),
                    Check.check_locations.is_(True),
                    Check.type == "http",
                )
            )
        )
        results = {
            (r.check_id, r.location_id): r
            for r in await session.scalars(select(LocationResult))
        }
    await _forget_stale_locations(session_factory, enabled)
    # прокси, который лег целиком, - один алерт про него, а не "недоступен из ..." по каждому сайту
    broken = await _confirm_broken(
        broken_location_ids(enabled, checks, results, min_fails=2), enabled, now
    )

    pending: list = []
    # (check_id, откуда не виден, с какого момента) — новое состояние. Копится
    # отдельно от pending: его надо сохранить независимо от того, ушёл алерт или
    # нет (см. ниже).
    state_updates: list[tuple[int, list[int] | None, datetime | None]] = []
    for c in checks:
        lines: list[str] = []  # строка на локацию: «🟢/🔴 Имя — доступен/недоступен»
        down_ids: list[int] = []
        n_up = n_down = 0
        thr = max(c.alert_after_failures, 1)  # столько же подряд-сбоев, как для основного алерта
        for loc in effective_locations(c, enabled):
            if loc.id in broken:
                # о сломанном прокси - отдельный алерт. Уже объявленную "недоступен отсюда" не
                # трогаем: ни нового алерта, ни ложного "снова доступен отовсюду"
                if c.loc_alerted and loc.id in c.loc_alerted:
                    lines.append(f"🔴 {loc.name} — недоступен")
                    down_ids.append(loc.id)
                    n_down += 1
                continue
            if not loc.url:  # прямая = основная проверка
                st, cf = c.last_status, c.consecutive_fails
            else:
                r = results.get((c.id, loc.id))
                st = r.status if r is not None else "unknown"
                cf = r.consecutive_fails if r is not None else 0
            if st in ("up", "degraded"):
                lines.append(f"🟢 {loc.name} — доступен")
                n_up += 1
            elif st == "down":
                # дебаунс: считаем локацию упавшей только после N подряд-сбоев,
                # иначе — транзиент (флап прокси), не двигаем набор и не алертим
                if cf < thr:
                    continue
                lines.append(f"🔴 {loc.name} — недоступен")
                down_ids.append(loc.id)
                n_down += 1
            # unknown (ещё не проверялось) — пропускаем
        down_ids.sort()
        prev = sorted(c.loc_alerted) if c.loc_alerted is not None else None
        # О чём уже уведомили — отдельно от того, что показываем: алерт может быть
        # придержан выдержкой или не доставлен, а интерфейс должен знать правду сразу.
        notified = sorted(c.loc_notified) if c.loc_notified is not None else None

        if n_up and n_down:  # частичная доступность → список локаций с кружками
            # Отсчёт ведём от НАЧАЛА состояния, а не от смены набора: если сперва
            # отвалилась одна точка, а через минуту вторая — сайт всё это время
            # виден не отовсюду, и заново ждать выдержку незачем.
            # _aware: sqlite отдаёт время без зоны, вычитать такое из now нельзя
            since = _aware(c.loc_partial_since) if c.loc_partial_since else now
            if down_ids != prev or c.loc_partial_since is None:
                state_updates.append((c.id, down_ids, since))
            if (now - since).total_seconds() >= _LOC_PARTIAL_SUSTAIN and down_ids != notified:
                pending.append(
                    ("locpart", c.name, "", "\n".join(lines), None, c.id,
                     ("loc_notified", down_ids), "")
                )
        elif not n_down:  # снова доступен отовсюду
            if prev is not None or c.loc_partial_since is not None:
                state_updates.append((c.id, None, None))
            # Отбой — только по алерту, который действительно был отправлен.
            if notified is not None:
                pending.append(("locrec", c.name, "", "", None, c.id, ("loc_notified", None), ""))
        elif not n_up and c.loc_partial_since is not None:
            # Не виден ниоткуда: полное падение ведёт основной инцидент, а здесь
            # важно обнулить отсчёт. Точки поднимаются по одной, и без сброса
            # выдержка была бы уже выдержана к моменту, когда встала первая из них —
            # то есть ровно на восстановлении и прилетал бы лишний алерт.
            state_updates.append((c.id, c.loc_alerted, None))

    # Состояние сохраняем ДО отправки и независимо от неё. loc_alerted — это не
    # только дедупликация алерта: на нём держатся счётчик «частично», чип с
    # именем точки в списке и сводка проблемных точек. Раньше оно применялось
    # внутри отправки, а та выходит в самом начале, если канал не настроен ИЛИ
    # алерты приглушены, — и панель молчала о том, что сайт не виден из части
    # точек, хотя все данные у неё были. Приглушить уведомления и ослепить
    # интерфейс — разные вещи.
    if state_updates:
        async with session_factory() as session:
            for check_id, value, since in state_updates:
                row = await session.get(Check, check_id)
                if row is not None:
                    row.loc_alerted = value
                    row.loc_partial_since = since
            await session.commit()

    if pending:
        try:
            await _send_alerts(session_factory, settings, pending, now)
        except Exception:  # noqa: BLE001
            log.exception("ошибка отправки локационных алертов")
    try:
        await _proxy_alerts(session_factory, settings, enabled, checks, results, broken, now)
    except Exception:  # noqa: BLE001
        log.exception("ошибка алерта о сломанном прокси локации")


# Подтверждение "сломан прокси": сам прокси проверяем запросом через него. Отвечает - значит,
# сайты недоступны из той страны по-настоящему (блокировка), и про них нужны обычные алерты.
# Ответ помним 5 минут, чтобы не дергать прокси каждый тик
_PROXY_CONFIRM_TTL = 300
_proxy_confirm: dict[int, tuple[float, bool]] = {}


async def _confirm_broken(suspect: set[int], enabled: list[Location], now: datetime) -> set[int]:
    out: set[int] = set()
    ts = now.timestamp()
    for loc in enabled:
        if loc.id not in suspect:
            _proxy_confirm.pop(loc.id, None)
            continue
        hit = _proxy_confirm.get(loc.id)
        if hit is None or ts - hit[0] >= _PROXY_CONFIRM_TTL:
            ok, _msg, _lat = await checks_exec.test_proxy(loc.url)
            hit = (ts, ok)
            _proxy_confirm[loc.id] = hit
        if not hit[1]:
            out.add(loc.id)
    return out


async def _proxy_alerts(
    session_factory, settings: Settings, enabled: list[Location], checks: list[Check],
    results: dict, broken: set[int], now: datetime,
) -> None:
    """Один алерт "прокси локации не отвечает" после выдержки и отбой, когда заработал.
    Подчиняется правилу "Частичная доступность (локации)": это его алерты он заменяет."""
    async with session_factory() as session:
        raw = await settings_store.get_raw(session, settings_store.LOC_BROKEN_KEY)
        if not broken and raw in (None, "", "{}"):
            return  # все прокси живы и не было - нечего ни слать, ни запоминать
        cfg = await settings_store.get_alert_config(session, settings)
        muted = await settings_store.get_muted(session)
        rule = (await settings_store.get_site_alert_rules(session)).get("locpart") or {}
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    names = {loc.id: loc.name for loc in enabled}
    new: dict[str, dict] = {}
    fires: list[tuple[str, str]] = []  # (id, текст)
    recs: list[tuple[str, str]] = []
    for lid in broken:
        key, cur = str(lid), state.get(str(lid)) or {}
        got = [r for c in checks if (r := results.get((c.id, lid))) is not None and r.status == "down"]
        err = Counter(r.message for r in got).most_common(1)[0][0] if got else ""
        since = cur.get("since") or now.isoformat()
        item = {"since": since, "err": err[:200], "notified": bool(cur.get("notified"))}
        new[key] = item
        if not item["notified"] and (now - datetime.fromisoformat(since)).total_seconds() >= _LOC_PARTIAL_SUSTAIN:
            name = html.escape(names.get(lid, "?"))
            fires.append((key, (
                f"🧭 Прокси локации «{name}» не отвечает: недоступны все сайты ({len(got)}), "
                f"которые через нее проверяются, а с самой панели часть из них отвечает."
                + (f"\nОшибка: {html.escape(err[:200])}" if err else "")
                + f"\nАлерты «недоступен из {name}» по сайтам не шлются, пока прокси не заработает."
            )))
    for key, cur in state.items():
        if key in new or not cur.get("notified"):
            continue
        if int(key) in names:
            recs.append((key, f"✅ Прокси локации «{html.escape(names[int(key)])}» снова работает"))
        # локацию убрали или выключили - отбой не нужен
    can_send = bool(rule.get("enabled", True)) and not muted and alerts.alerts_enabled(cfg)
    msgs = [alerts.Msg(t, "sites", "", kind="locpart", target=names.get(int(k), ""), recovery=False)
            for k, t in fires]
    msgs += [alerts.Msg(t, "sites", "", kind="locpart", target=names.get(int(k), ""), recovery=True)
             for k, t in recs]
    delivered = bool(msgs) and can_send and await alerts.dispatch(
        cfg, True, msgs, int(cfg.get("flood_threshold", 6)), parse_mode="HTML",
        session_factory=session_factory,
    )
    if delivered:
        for key, _t in fires:
            new[key]["notified"] = True
    else:
        for key, _t in recs:  # отбой не дошел - попробуем на следующем тике
            new[key] = state[key]
    if new != state:
        async with session_factory() as session:
            await settings_store.set_raw(session, settings_store.LOC_BROKEN_KEY, json.dumps(new))


_SRV_LABEL = {
    "offline": "на связи", "cpu": "CPU", "mem": "RAM", "disk": "диск",
    "temp": "температура", "throttle": "троттлинг",
    "conntrack": "conntrack", "disktemp": "температура диска",
    "db_conn": "коннекты СУБД",
    "backup_missing": "бэкап", "backup_failed": "бэкап", "backup_stale": "свежесть бэкапа",
    "backup_dump": "дамп СУБД", "backup_dump_space": "место под дампы",
    "backup_cron": "дамп-CronJob", "backup_custom": "свой бэкап", "clock": "время", "dns": "DNS",
    "kube_expiry": "сроки Kubernetes", "flux_down": "доставка Flux", "flux_stale": "репозиторий чартов",
    "kube_pod": "поды kubernetes",
    "kube_cron": "CronJob kubernetes",
    "web_5xx": "ошибки 5xx",
    "disk_health": "диски",
    "inode": "inode",
    "disk_forecast": "прогноз заполнения",
    "units": "юниты systemd",
    "cpu_spin": "процессы в пустом цикле",
    "backup_growth": "прирост бэкапа",
    "docker_sock": "docker-сокет у внешнего прокси",
    "docker_risk": "контейнеры с ключами от хоста",
    "access_risk": "второй root или пустой пароль",
    "exposed": "открытые в интернет сервисы",
    "pressure": "давление на ресурсы",
    "net_health": "сеть ноды",
    "link": "связь между серверами",
    "traffic": "трафик",
    "kube_node": "ноды kubernetes",
    "backup_restore": "проверка восстановления",
    "gpu": "GPU",
    "gpu_heat": "перегрев GPU",
    "llm": "LLM-сервисы",
    "queue_stuck": "очереди",
    "queue_failed": "упавшие задачи",
}

# Единицы пороговых метрик. Нужны отбою: голое «снова в норме» не отвечает на
# первый вопрос инженера — сколько освободилось. Показываем «диск снова в норме:
# 82% (было 90%)», где «было» — значение на момент срабатывания.
_SRV_UNIT = {"cpu": "%", "mem": "%", "disk": "%", "inode": "%", "conntrack": "%", "db_conn": "%",
             "temp": "°C", "disktemp": "°C"}


# Поды, которые контроллер ОБЯЗАН держать живыми. Поды Job/CronJob сюда не берём: они
# на то и одноразовые, а их ImagePullBackOff - обычный мусор отработавших кронов (на
# stats-dev таких 19 штук). Поды без контроллера - ручные, их никто не поднимет.
_POD_OWNERS = frozenset({"Deployment", "StatefulSet", "ReplicaSet", "DaemonSet"})
_POD_FINISHED = frozenset({"Succeeded", "Failed"})


def kube_bad_pods(rep: dict) -> list[dict]:
    """Поды, которые должны работать и не работают: не поднялись, падают в цикле или
    работают, но не готовы. Выдержка (alert_sustain_seconds) отсекает обычный выкат:
    новый под несколько минут не готов, и это не авария."""
    out = []
    for p in ((rep.get("kube") or {}).get("pods") or []):
        if p.get("owner") not in _POD_OWNERS or p.get("phase") in _POD_FINISHED:
            continue
        # ready is False, а не falsy: агент, который поля не шлёт, иначе дал бы алерт
        # про каждый работающий под
        if p.get("phase") == "Running" and p.get("ready") is not False:
            continue
        out.append(p)
    return out


def bad_pods_text(pods: list[dict], limit: int = 4) -> str:
    """«default/postgres-0 (CrashLoopBackOff, 83 рестарта), и ещё 2». Причина обязательна:
    CreateContainerConfigError и ImagePullBackOff чинятся совершенно по-разному."""
    items = []
    for p in pods[:limit]:
        why = p.get("reason") or p.get("phase") or "?"
        if p.get("msg"):
            # почему не встает в расписание (агент 2.26): "0/3 nodes are available: ..."
            why += ": " + str(p["msg"])[:140]
        restarts = int(p.get("restarts") or 0)
        tail = f", {restarts} {_plural(restarts, 'рестарт', 'рестарта', 'рестартов')}" if restarts >= 5 else ""
        items.append(f"{p.get('ns', '?')}/{p.get('name', '?')} ({why}{tail})")
    if len(pods) > limit:
        items.append(f"и ещё {len(pods) - limit}")
    return ", ".join(items)


def _recovery_detail(key: str, st: dict, ctx: dict) -> str:
    """Текст отбоя порогового алерта. Прежнее значение берём из alert_state
    («<тип>_val», кладётся вместе с самим срабатыванием); если его нет — алерт
    объявлен ещё старой версией, тогда показываем хотя бы текущее."""
    if key == "web_5xx":
        return "ошибок 5xx нет уже час, доля ниже порога"
    if key == "disk_health":
        return "диски снова в порядке: RAID собран, неисправных и пропавших дисков нет"
    if key == "gpu":
        return "GPU снова в порядке: драйвер отвечает, все карты на месте, свежих ошибок Xid нет"
    if key == "gpu_heat":
        return "GPU остыли, замедления из-за температуры нет"
    if key == "llm":
        return "LLM-сервисы снова в норме: отвечают, очереди нет, задержки обычные"
    if key == "queue_stuck":
        return "очереди снова разбираются"
    if key == "queue_failed":
        return "задачи больше не падают пачками: за час упало меньше половины порога"
    if key == "workers":
        return "воркеры снова работают"
    if key == "cron_stuck":
        return "кроны больше не наслаиваются и не висят"
    if key == "log_errors":
        return "ошибок в логах снова обычно: за час меньше половины порога"
    if key == "fpm":
        return "пулы PHP-FPM снова справляются: запросы не ждут воркеров и не убиваются по таймауту"
    if key == "disk_forecast":
        return "диски больше не грозят заполниться в ближайшие дни: рост замедлился или место освободили"
    if key == "units":
        return "упавших юнитов больше нет"
    if key == "cpu_spin":
        return "процессов, крутивших CPU вхолостую, больше нет"
    if key == "backup_growth":
        return "прирост бэкапа снова обычный"
    if key == "docker_sock":
        return "внешний прокси больше не держит docker-сокет"
    if key == "docker_risk":
        return "контейнеров с ключами от хоста больше нет"
    if key == "flux_stale":
        return "репозиторий чартов снова отвечает"
    if key == "kube_cron":
        return "CronJob кластера снова отрабатывают"
    if key == "exposed":
        if ctx.get("own"):
            return ("проверка шла с адреса панели, а файрвол ноды может пускать панель и закрывать "
                    "порты для остальных: по такой проверке панель больше не алертит, нужна "
                    "локация-прокси в другой сети")
        return "опасных и служебных портов, открытых в интернет, больше нет"
    if key == "pressure":
        return "сервер снова дышит: процессы не ждут память и диск, зависших нет"
    if key == "net_health":
        return "сеть ноды снова в порядке: потерь, переполнений и ошибок нет"
    if key == "link":
        return "связь с другими серверами снова в порядке"
    if key == "traffic":
        return "трафик снова в пределах лимита тарифа"
    if key == "kube_node":
        return "ноды кластера снова Ready, давления нет"
    if key == "mem_leak":
        return "память больше не растет день за днем"
    if key == "listen_gone":
        return "пропавшие порты снова слушаются или сняты с наблюдения"
    if key == "disk_slow":
        return "задержка диска снова в норме"
    if key == "web_cert":
        return "сертификаты веб-серверов снова в сроке"
    if key == "web_config":
        return "конфиги веб-серверов снова проходят проверку"
    if key == "db_health":
        return "базы снова в порядке: реплики догнали, долгих транзакций и блокировок нет"
    if key == "ssh_risk":
        return "SSH больше не пускает root по паролю, перебора без защиты нет"
    if key == "access_risk":
        return "второго root и учеток с пустым паролем больше нет"
    if key == "kube_restarts":
        return "поды восстановлены, частых перезапусков нет"
    if key == "docker_restarts":
        return "контейнеры восстановлены, частых перезапусков нет"
    if key == "backup_restore":
        return "проверка восстановления снова проходит: файлы и дампы из снапшота читаются"
    label = _SRV_LABEL[key]
    unit, val = _SRV_UNIT.get(key), ctx.get("value")
    if not unit or val is None:
        return f"снова в норме ({label})"
    was = st.get(f"{key}_val")
    return f"{label} снова в норме: {val}{unit}" + (
        f" (было {was}{unit})" if was is not None else ""
    )

# Тепловой троттлинг: алертим только на УСТОЙЧИВЫЙ (недоохлаждение), а не на
# одиночные микро-спайки счётчика. Интервал засчитывается, лишь если ядро горячее
# (≥ FLOOR °C — троттлинг при 49-77°C это не тепловая проблема), и нужно ≥ STREAK
# таких интервалов подряд, чтобы отправить алерт.
_THROTTLE_TEMP_FLOOR = 80.0  # °C: ниже — «холодный» троттлинг из счётчика = шум
_THROTTLE_MIN_STREAK = 3  # интервалов подряд с реальным троттлингом до алерта
# Дебаунс «мгновенных» метрик (CPU/RAM/темп/conntrack/темп диска): алертим, только
# если превышение ДЕРЖИТСЯ дольше alert_sustain_seconds (по умолч. 15 мин). Считаем
# ПО ВРЕМЕНИ (а не по тикам) — устойчиво к частоте отчётов/тиков: запоминаем момент
# начала «жарки» ({тип}_since), сбрасываем как только метрика вернулась в норму или
# сервер оффлайн (данные протухли). Одиночные спайки «моргнуло и прошло» не шлём.
_SUSTAIN_DEFAULT = 900
# Сколько должна продержаться частичная доступность, прежде чем о ней сообщать.
# Точки проверки ходят каждая на своей каденции, поэтому сайт, поднявшийся после
# падения, какое-то время виден из одних и не виден из других — это фаза
# восстановления, а не проблема с регионом. Без выдержки каждое восстановление
# давало лишнюю пару «недоступен из N» + «снова доступен отовсюду».
_LOC_PARTIAL_SUSTAIN = 300

# Как назвать истекающую сущность в алерте. Путь к файлу инженеру мало что говорит,
# пока не сказано, что именно этот файл держит.
_KUBE_KIND = {
    "cluster-cert": "сертификат control-plane",
    "kubeconfig": "kubeconfig",
    "kubelet-cert": "сертификат kubelet",
    "flux-token": "токен Flux в секрете",
    "secret-cert": "TLS-сертификат в секрете",
}

# Что делать — отдельно для «ещё не истёк» и «уже истёк». Одной короткой строкой:
# алерт без действия бесполезен, но и объяснять в нём, почему кластер ещё жив, тоже
# не нужно — за этим идут в панель, а в сообщении важны срок и следующий шаг.
_KUBE_ADVICE = {
    ("cluster-cert", False): "перевыпустите заранее",
    ("cluster-cert", True): "перевыпускайте: control-plane не переживёт рестарт",
    ("kubeconfig", False): "перевыпустите",
    ("kubeconfig", True): "этим файлом в кластер уже не зайти — перевыпустите",
    ("kubelet-cert", False): "должен ротироваться сам: если дата близко, ротация встала",
    ("kubelet-cert", True): "нода отвалится от кластера — чините kubelet",
    ("flux-token", False): "выпустите новый токен и обновите секрет",
    ("flux-token", True): "Flux уже не тянет изменения — новый токен в секрет",
    # До срока совета нет: "обновите до этой даты" повторяло саму дату и только
    # удлиняло сообщение.
    ("secret-cert", False): "",
    # Прежний текст обещал поломку «при рестарте пода» — неправда для главного
    # случая: секрет из ingress/gateway контроллер читает сам и отдаёт клиентам
    # как есть, поэтому браузер ругается сразу, задолго до всякого рестарта.
    ("secret-cert", True): "TLS уже не проходит проверку — обновите или удалите",
}
# Ready=False с этими причинами — не поломка, а работа: Flux тянет артефакт или
# докатывает релиз. Алертить на них — гарантированно приучить игнорировать канал.
_FLUX_TRANSIENT = frozenset({"Progressing", "Reconciling", "ProgressingWithRetry", "Unknown"})

# Причина отказа Flux по-русски и куда смотреть. Английский reason сам по себе инженеру
# мало что даёт: «InstallFailed» не говорит, чинить чарт, кластер или права. Пары
# (что случилось, где смотреть) собраны по тому, что реально прилетало с парка.
_FLUX_REASON = {
    "GitOperationFailed": ("не может сходить в Git — обычно истёк или отозван токен",
                           "проверьте секрет с доступом у источника"),
    "AuthenticationFailed": ("отказ в доступе к источнику",
                             "проверьте секрет с доступом у источника"),
    "InstallFailed": ("Helm не смог поставить релиз", "kubectl -n {ns} get pods"),
    "UpgradeFailed": ("Helm не смог обновить релиз", "kubectl -n {ns} get pods"),
    "TestFailed": ("тесты чарта не прошли", "kubectl -n {ns} get pods"),
    "RemediationFailed": ("откат после неудачного выката тоже не удался",
                          "kubectl -n {ns} get helmrelease {name}"),
    "HealthCheckFailed": ("ресурсы не поднялись после выката",
                          "kubectl -n {ns} describe kustomization {name}"),
    "BuildFailed": ("не собираются манифесты", "проверьте kustomization в репозитории"),
    "ArtifactFailed": ("не смог скачать артефакт источника",
                       "kubectl -n {ns} get gitrepositories,ocirepositories"),
    "ReconciliationFailed": ("не смог применить изменения в кластер",
                             "kubectl -n {ns} describe kustomization {name}"),
    "DependencyNotReady": ("ждёт другую сборку", ""),
    "PostRenderingFailed": ("не отработал post-renderer", "проверьте патчи релиза"),
}

# Преамбулы, которые Flux ставит перед собственно причиной. Они уже сказаны в
# заголовке алерта («Helm не смог обновить релиз») и в строке с именем ресурса,
# поэтому в тексте это просто шум, съедающий место до сути.
_FLUX_STRIP = (
    re.compile(r"^Helm (?:install|upgrade|uninstall|rollback) failed for release \S+ "
               r"with chart \S+:\s*"),
    re.compile(r"^health check failed after [\d.]+[a-z]*s:\s*"),
    re.compile(r"^failed early due to stalled resources:\s*"),
    re.compile(r"^failed to checkout and determine revision:\s*"),
    re.compile(r"^unable to clone [^:]*:\s*"),
    re.compile(r"^reconciliation failed:\s*"),
)

# «[Job/k8s-b-prod/k8s-b-migrations status: 'Failed']» — так Flux называет объект,
# который не поднялся. Человеку нужнее «Job k8s-b-migrations: Failed».
_FLUX_OBJ = re.compile(r"\[(\w+)/[^/\]]+/([^\s\]]+) status: '([^']+)'\]")


# Подсказка по тексту ошибки. reason у источников бывает общий ("Failed"), и по нему не понять,
# что чинить: 07.10.2026 HelmRepository cert-manager на двух РФ-нодах упал с "dial tcp
# 8.6.112.0:443: i/o timeout", и алерт пришел без единого слова о причине. Побеждает первое
# совпадение, поэтому узкие образцы стоят выше общих.
_FLUX_MSG_HINTS = (
    # Cloudflare отдает российским резолверам эти адреса, а из РФ они недоступны
    (re.compile(r"dial tcp (?:8\.6\.112|8\.47\.69)\.\d+:\d+"),
     "это адреса Cloudflare, из РФ они недоступны: переведите источник на OCI вне Cloudflare "
     "(чарты cert-manager - oci://quay.io/jetstack/charts)"),
    (re.compile(r"no such host"), "имя источника не резолвится на ноде, проверьте DNS"),
    (re.compile(r"\b(?:401|403)\b|unauthorized|authentication required|access denied", re.I),
     "источник отказал в доступе, проверьте токен в секрете источника"),
    (re.compile(r"x509|certificate", re.I), "не прошла проверка TLS-сертификата источника"),
    (re.compile(r"i/o timeout|connection timed out|connection refused|context deadline exceeded", re.I),
     "источник не отвечает с ноды: проверьте, открыт ли он из этой сети"),
)


def _flux_msg_hint(msg: str) -> str:
    """Что делать, судя по тексту ошибки Flux. Пусто, если текст ни на что не похож."""
    for rx, hint in _FLUX_MSG_HINTS:
        if rx.search(msg or ""):
            return hint
    return ""


def _flux_detail(msg: str, limit: int = 150) -> str:
    """Сообщение Flux без преамбул, обрезанное ПО ГРАНИЦЕ СЛОВА.

    Резать вслепую нельзя: в ленте висело «would exceed context de.» — обрывок,
    похожий на опечатку и не объясняющий ничего."""
    msg = " ".join((msg or "").split())
    changed = True
    while changed:  # преамбулы бывают вложены одна в другую
        changed = False
        for pat in _FLUX_STRIP:
            new = pat.sub("", msg)
            if new != msg:
                msg, changed = new, True
    msg = _FLUX_OBJ.sub(lambda m: f"{m.group(1)} {m.group(2)}: {m.group(3)}", msg)
    if len(msg) <= limit:
        return msg
    cut = msg[:limit].rsplit(" ", 1)[0]
    return (cut or msg[:limit]) + "…"
# Сколько молчания СВЕРХ offline_after нужно для алерта (в интерфейсе нода посереет
# сразу, а в Telegram уйдёт только устойчивый обрыв). 3 минуты = 12 пропущенных
# отчётов подряд: разовая перегрузка канала столько не держится.
_OFFLINE_ALERT_EXTRA = 180

# Docker-контейнеры. Crash-loop: сколько РАЗ RestartCount должен вырасти за окно,
# чтобы счесть «постоянно ребутается» (одиночный рестарт при деплое не в счёт).
# «Упал»: алертим только у контейнеров с restart-policy — намеренно остановленные
# и one-shot (policy=no) не трогаем, это и есть защита от ложных срабатываний.
_DOCKER_LOOP_WINDOW = 900  # сек: окно подсчёта рестартов (15 мин)
_DOCKER_LOOP_MIN = 3  # приростов RestartCount за окно → crash-loop
_DOCKER_RESTART_POLICIES = frozenset({"always", "unless-stopped", "on-failure"})
_DOCKER_DOWN_STATES = frozenset({"exited", "dead"})
# С какими кодами контейнер обычно выходит по docker stop: 0 - сам корректно завершился по
# SIGTERM, 143 - убит SIGTERM, 137 - добит SIGKILL после таймаута остановки
_DOCKER_STOP_CODES = frozenset({0, 137, 143})
_EXITED_RE = re.compile(r"^Exited \((-?\d+)\)")


def docker_exit_code(c: dict) -> int | None:
    """Код выхода остановленного контейнера: поле агента (2.22) или строка статуса docker
    ("Exited (0) 17 minutes ago")."""
    if isinstance(c.get("exit"), int):
        return c["exit"]
    m = _EXITED_RE.match(str(c.get("status") or ""))
    return int(m.group(1)) if m else None


def docker_down_why(c: dict) -> str | None:
    """Почему лежащий контейнер - авария (текст для алерта), или None, если это не авария.

    docker stop не снимает restart-policy, поэтому "не работает при policy=always" само по себе
    не авария: упавший контейнер с такой политикой докер поднимает сам за секунды. Долго лежать
    он может, если его остановили руками (разработчик погасил воркеры на время работы, а панель
    подняла тревогу) или если докер не смог его запустить - причину докер
    пишет в State.Error, агент 2.22 отдает ее в err. У on-failure:N - еще когда кончились
    перезапуски. Контейнеры без restart-policy (one-shot, policy=no) не трогаем вовсе."""
    state = (c.get("state") or "").lower()
    policy = (c.get("policy") or "").lower()
    if policy not in _DOCKER_RESTART_POLICIES:
        return None
    err = str(c.get("err") or "").strip()
    # created с ошибкой: compose up не смог его запустить (порт занят, нет каталога для mount)
    if state not in _DOCKER_DOWN_STATES and not (state == "created" and err):
        return None
    if state == "dead":
        return "состояние dead: докер не смог его остановить или убрать"
    if err:
        return f"докер не смог его запустить: {err}"
    code = docker_exit_code(c)
    if policy == "on-failure" and code == 0:
        return None  # отработал и вышел сам: on-failure перезапускает только после ошибки
    if "exit" in c:
        # Агент 2.22+ видит State.Error, и ее нет. При always/unless-stopped это остановка
        # руками; при on-failure тоже, пока перезапуски не кончились: иначе докер поднимал бы его.
        mx, rc = int(c.get("max_retry") or 0), int(c.get("restarts") or 0)
        if policy == "on-failure" and mx and rc >= mx:
            oom = ", убит OOM" if c.get("oom") else ""
            return f"перезапуски кончились ({rc} из {mx}), код выхода {code}{oom}"
        return None
    # агент старше 2.22: ошибку запуска не видно, судим по коду выхода
    if code in _DOCKER_STOP_CODES:
        return None
    return f"остановился с кодом {code}" if code is not None else "не работает"

# Бэкап считается «не свежим», если последний успешный прогон старше этого порога.
# Бэкапы обычно ежедневные → 2 дня = ≥2 пропуска подряд, это уже требует внимания.
_BACKUP_STALE_SECONDS = 2 * 86400
# «бэкап не настроен» — не инцидент, а задача «на сделать»: даём сутки и на настройку
# новой ноды, и на переконфигурацию существующей, прежде чем беспокоить инженера
_BACKUP_MISSING_GRACE = 86400
# Тишина для только что добавленного сервера (см. evaluate_servers): helper'ы ставятся, отчеты неполные
_NEW_SERVER_QUIET = 900
# Сколько недоступных серверов за тик панель проверяет сама (app/liveness.py): каждая проверка -
# до нескольких секунд, а при обрыве сети у самой панели молчат все разом
_LIVENESS_PER_TICK = 6
# Репозиторий на бэкап-сервере «устарел», если в него давно не принимали бэкап.
# 3 дня для ежедневных бэкапов; разовые/неактуальные репо глушатся мьютом.
_BACKUP_REPO_STALE_SECONDS = 3 * 86400
# Алерт «репозитории требуют внимания» НЕ срочный: шлём при появлении проблемы и
# напоминаем не чаще раза в сутки (иначе спам, т.к. набор проблемных репо флапает).
_BACKUP_REPO_REALERT = 86400
# Лок в репозитории — НОРМА, пока идёт бэкап: restic держит его от начала до конца и
# освежает раз в 5 минут. Проблема — лок, который перестали освежать (процесс умер,
# репо осталось заблокированным). Порог с большим запасом к интервалу освежения.
# Без этого каждый бэкап, попавший в момент опроса, давал алерт «требуют внимания»
# и через минуту «снова в норме».
_BACKUP_LOCK_STUCK_SECONDS = 30 * 60
# Дамп СУБД снимается ПЕРЕД каждым файловым бэкапом (ExecStartPre). Значит после
# успешного бэкапа дамп должен быть почти таким же свежим. Если последний бэкап
# новее дампа больше чем на этот порог — дамп падает при каждом прогоне (сломался
# движок, сменился пароль, упал контейнер), а файловый бэкап это НЕ ловит: дамп
# цепляется через ExecStartPre=- («минус» = провал дампа бэкап не отменяет).
# ПОРОГ НЕ ДОЛЖЕН СОВПАДАТЬ С ПЕРИОДОМ БЭКАПА. Было ровно 86400 при суточных бэкапах —
# и панель ловила ложняк на стыке циклов: last_backup_ts агент читает ЖИВЬЁМ из systemd,
# а дампы — из /var/lib/kervax/backup-config.json, который root-cron обновляет раз в минуту.
# В окне «бэкап уже стартовал, конфиг ещё вчерашний» лаг = сутки + джиттер (на одной ноде
# 24ч07м) → алерт, а через минуту cron обновил файл → «снова в норме». Двое суток дают
# запас в целый цикл; поломка дампа — процесс медленный, ловить её за минуты незачем.
_BACKUP_DUMP_LAG_SECONDS = 2 * 86400

# Нода без файлового бэкапа: helper снимает дамп своим таймером раз в сутки (03:00 плюс до
# 15 минут разброса), и сверить дамп можно только с часами. Двое суток — чтобы одиночный
# пропуск (ночная перезагрузка, под в рестарте) не будил: следующей ночью дамп снимется
# сам. Час сверху — на разброс таймера, иначе при одном пропуске возраст на пару минут
# переходил бы порог прямо перед следующим удачным запуском, и алерт мигал бы.
_DUMP_LOCAL_LAG_SECONDS = 2 * 86400 + 3600

# Почему дамп считается сломанным — хвост текста алерта backup_dump ({reason}).
_DUMP_REASON_BACKUP = "бэкап идёт, а дамп не обновился — файловый снапшот живой базы может не восстановиться"
_DUMP_REASON_LOCAL = "свежего дампа нет больше двух суток, а файлового бэкапа на ноде нет"


def _dump_label(d: dict) -> str:
    eng = d.get("engine") or "?"
    cont = d.get("container") or ""
    return f"{eng}@{cont}" if cont else eng


def dump_local_stale(d: dict, now_ts: float) -> bool:
    """Сломан ли дамп на ноде без файлового бэкапа (k8s-c, 17.09.2026): свежего
    файла нет дольше _DUMP_LOCAL_LAG_SECONDS ни с последнего дампа, ни с включения.
    Включение (enabled_ts — mtime скрипта дампа, он переписывается и при перенастройке)
    даёт отсрочку: первый дамп снимется только ближайшей ночью. Пропуск из-за места —
    отдельный алерт, здесь он не считается. Раньше такие дампы не проверялись вовсе:
    проверка шла только от времени бэкапа, а бэкапа на ноде нет."""
    if d.get("skipped"):
        return False
    last = (d.get("last_ts") or 0) if (d.get("files") or 0) > 0 else 0
    ref = max(last, d.get("enabled_ts") or 0)
    return ref > 0 and now_ts - ref > _DUMP_LOCAL_LAG_SECONDS


def backup_run_start(bk: dict) -> int:
    """Когда стартовал последний ЗАВЕРШЕННЫЙ бэкап. Дампы снимаются в самом начале прогона
    (ExecStartPre), поэтому дамп, включенный посреди долгого бэкапа, в этот прогон не попал. По
    концу прогона (last_backup_ts) судить нельзя: на одной ноде бэкап шел 52 минуты, дампы
    включили через 3 и 9 минут после его старта, и панель решила, что бэкап после включения
    прошел, а файлов нет - алерт "дамп не обновляется" и оранжевое в покрытии.
    started_ts - старт последнего запуска; если он позже конца, идет новый прогон, и старт
    завершенного оцениваем по длительности restic."""
    last = bk.get("last_backup_ts") or 0
    if not last:
        return 0
    st = bk.get("started_ts") or 0
    if st and st <= last:
        return st
    return max(0, last - (bk.get("duration_sec") or 0))


def _dump_problems(bk: dict) -> list[str]:
    """Движки, чей дамп настроен, но не снимается. Два признака поломки: файлов нет
    вовсе (ни один дамп не удался) либо дамп отстал от последнего успешного бэкапа
    (перестал сниматься). НАМЕРЕННЫЙ пропуск из-за места сюда НЕ входит — для него
    отдельный алерт с actionable-текстом (_dump_skipped). Возвращает метки «движок»
    (с контейнером, если баз одного типа несколько). Пусто, если всё свежо."""
    last_backup = bk.get("last_backup_ts") or 0
    run_start = backup_run_start(bk)
    out: list[str] = []
    for d in bk.get("dumps") or []:
        if d.get("skipped"):
            continue  # пропущен намеренно (мало места) — это отдельный сигнал, не «падает»
        label = _dump_label(d)
        files = d.get("files") or 0
        dts = d.get("last_ts") or 0
        enabled = d.get("enabled_ts") or 0
        # «файлов нет» — проблема ТОЛЬКО если бэкап уже проходил ПОСЛЕ включения дампа,
        # а файла всё равно нет (дамп реально падает). Сразу после включения файлов ещё
        # нет законно: первый дамп снимется в ближайший бэкап. enabled_ts — mtime скрипта
        # дампа. Без этой проверки панель ныла «файлов нет» в первые же секунды. Сравниваем
        # со стартом прогона, а не с концом: дампы снимаются в начале (backup_run_start).
        if files == 0 or dts == 0:
            if enabled and run_start > enabled:
                out.append(label)  # бэкап начался после включения, а дампа нет → падает
            # иначе: только включили, расписание ещё не наступило — молчим
        elif last_backup and (last_backup - dts) > _BACKUP_DUMP_LAG_SECONDS:
            out.append(label)  # бэкап прошёл, а дамп остался старым → падает
    return sorted(out)


def _dump_skipped(bk: dict) -> tuple[list[str], int]:
    """Дампы, пропущенные из-за нехватки места (helper выставил флаг skipped). Возвращает
    (метки движков, минимальный % свободного среди пропущенных) — процент идёт в текст
    алерта. Это НЕ поломка дампа, а защита от переполнения: диск надо расчистить."""
    labels: list[str] = []
    min_free = 100
    for d in bk.get("dumps") or []:
        if not d.get("skipped"):
            continue
        labels.append(_dump_label(d))
        fp = d.get("skip_free_pct")
        if isinstance(fp, int) and 0 <= fp < min_free:
            min_free = fp
    return sorted(labels), (min_free if labels else 0)


# ключи образов СУБД — синхронны _DB_IMAGES в api/servers.py: опознаём тот же набор
# дамп-CronJob'ов, что аудит показывает как «дамп настроен» (иначе мониторили бы не то).
_DUMP_CRON_DB_KEYS = (
    "postgres", "postgis", "timescale", "mysql", "mariadb", "percona", "mongo",
    "clickhouse", "elasticsearch", "opensearch", "redis", "valkey", "influxdb",
    "victoriametrics", "etcd", "cockroach", "cassandra", "zookeeper", "kafka",
    "neo4j", "rabbitmq", "couchdb", "mssql", "sqlserver", "prometheus", "minio",
    "vault", "consul", "grafana",
)
# «в процессе» с запасом: запланированный прогон не считаем упавшим ещё 2 ч (длительный дамп)
_CRON_DUMP_SLACK_SECONDS = 2 * 3600


def _looks_dump_cron(cj: dict) -> bool:
    """Тот же матч, что _backup_coverage.existing: образ СУБД + намёк на дамп в имени/образе."""
    nm = (cj.get("name") or "").lower()
    img = (cj.get("image") or "").lower()
    looks_backup = any(w in nm for w in ("backup", "dump", "pgdump", "mysqldump"))
    return any(k in img and (looks_backup or k in nm) for k in _DUMP_CRON_DB_KEYS)


def _cron_interval_seconds(sched: str) -> int:
    """Грубая оценка интервала cron-расписания (5 полей) в секундах. Неизвестно → сутки.
    Нужна, чтобы порог «давно нет успеха» был адекватен частоте (ежечасный ≠ ежедневный)."""
    p = (sched or "").split()
    if len(p) != 5:
        return 86400
    minute, hour, dom, _mon, dow = p
    if minute.startswith("*/"):
        return max(int(minute[2:] or 1) * 60, 60)
    if minute == "*":
        return 60
    if hour == "*":
        return 3600
    if hour.startswith("*/"):
        return max(int(hour[2:] or 1) * 3600, 3600)
    if dow != "*":
        return 7 * 86400          # по дню недели → еженедельно
    if dom.startswith("*/"):
        return max(int(dom[2:] or 1) * 86400, 86400)
    if dom != "*":
        return 28 * 86400         # конкретное число месяца → ~ежемесячно
    return 86400                  # M H * * * → ежедневно


def cron_job_issues(rep: dict, now: datetime, dumps: bool) -> dict[str, str]:
    """Проблемные CronJob кластера: {ns/имя: причина}. dumps=True - только дамп-CronJob'ы
    (у них и "приостановлен" - проблема: бэкап встал), иначе все остальные (приостановленный
    обычный крон - чье-то решение, не поломка). Идущий прямо сейчас прогон (active) не трогаем,
    если он не завис: идет дольше двух интервалов (минимум сутки), а новых запусков нет. Так
    стоял descheduler раз в 5 минут, у которого прогон "шел" 132 дня.
    Порог несвежести - 2 интервала расписания (минимум 2ч): daily-крон молчит сутки, но не
    пропустит "не отработал 2 дня" - даже если CronJob вообще перестал стартовать (тогда
    last_schedule и last_success замирают вместе, и сравнение ls-ok эту дыру не ловит)."""
    out: dict[str, str] = {}
    nowts = now.timestamp()
    for cj in ((rep.get("kube") or {}).get("cronjobs") or []):
        if _looks_dump_cron(cj) != dumps:
            continue
        name = f"{cj.get('ns', '?')}/{cj.get('name', '?')}"
        if cj.get("suspend"):
            if dumps:
                out[name] = "приостановлен"
            continue
        ls = int(cj.get("last_schedule") or 0)
        ok = int(cj.get("last_success") or 0)
        every = _cron_interval_seconds(cj.get("schedule", ""))
        if cj.get("active"):
            if ls and nowts - ls > max(every * 2, 86400):
                out[name] = f"прогон идет {round((nowts - ls) / 86400, 1)} дн, новых запусков нет"
            continue  # прогон идёт — рано паниковать
        stale_after = max(every * 2, 2 * 3600)
        if ok > 0 and nowts - ok > stale_after:
            what = "дампа" if dumps else "прогона"
            out[name] = f"нет успешного {what} {round((nowts - ok) / 86400, 1)} дн"
        elif ls and (ok == 0 or ls - ok > _CRON_DUMP_SLACK_SECONDS):
            out[name] = "последний прогон не удался"
    return out


def _cron_dump_problems(rep: dict, now: datetime) -> list[str]:
    """Проблемные дамп-CronJob'ы строками "ns/имя (причина)". Панель их раньше только
    детектила - теперь мониторит (правило: любой настроенный бэкап должен алертить)."""
    return sorted(f"{n} ({why})" for n, why in cron_job_issues(rep, now, dumps=True).items())


def _backup_problem_repos(
    bs: dict, muted: set, now: datetime, extra: dict | None = None,
    names: set[str] | None = None, archive: dict | None = None,
) -> list[str]:
    """Имена НЕ заглушенных репо бэкап-сервера, требующих внимания: битые (нет config)
    или устаревшие (давно не принимали бэкап). Отсортировано.
    Висячий лок сюда больше не входит: у него свой алерт backup_lock и свой порог (сутки).
    Остался только лок от helper'а без времени лока: сколько он висит, не узнать.
    Репозитории в архиве не в счет: новых бэкапов в них и не ждут."""
    out = []
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        if not name or name in muted or archived_repo(archive, name):
            continue
        if repo_reason(r, now, extra, names):
            out.append(name)
    return sorted(out)


def _backup_repo_reason(r: dict, now: datetime, cadence: int = 0, monitored: bool = True) -> str:
    """Что с репозиторием, словами для алерта backup_repo. Пусто - все в порядке.

    cadence - обычный интервал между бэкапами (backup_cadence), 0 - не известен. Тогда порог
    прежний, 3 дня. С известным ритмом бэкап считается опоздавшим через интервал и еще
    четверть его (не меньше 2 часов): ежедневный - через 30 часов. У клиента с агентом в
    панели (monitored) сломанный бэкап раньше заметит сам агент, там ритм только раздвигает
    порог для редких бэкапов: недельный не устаревает через 3 дня."""
    if not r.get("valid"):
        return "нет config"
    last = r.get("last_activity") or 0
    age = now.timestamp() - last if last > 0 else 0
    if cadence > 0:
        late_after = cadence + max(cadence // 4, 2 * 3600)
        limit = max(_BACKUP_REPO_STALE_SECONDS, late_after) if monitored else late_after
    else:
        limit = _BACKUP_REPO_STALE_SECONDS
    if last > 0 and age > limit:
        if cadence > 0:
            return f"бэкап опоздал: обычно {_every(cadence)}, последний {_ago(age)} назад"
        return f"нет новых бэкапов {int(age // 86400)} дн"
    if r.get("locked") and not (r.get("lock_ts") or 0):
        return "залочен"
    return ""


def repo_reason(r: dict, now: datetime, extra: dict | None = None, names: set[str] | None = None) -> str:
    """_backup_repo_reason с ритмом из блока helper'а (0.28+) и с тем, есть ли клиент в панели.
    names - имена нод панели (panel_server_names); None - не знаем, считаем клиента своим."""
    name = r.get("name") or ""
    o = ((extra or {}).get("repos") or {}).get(name)
    cadence = backup_cadence(o.get("recent") if isinstance(o, dict) else None)
    monitored = names is None or name.lower() in names
    return _backup_repo_reason(r, now, cadence, monitored)


# Снапшоты одного прогона (файлы и дампы отдельными снапшотами, повтор после сбоя) ближе
# друг к другу, чем на полчаса: это один бэкап.
_RUN_GAP = 30 * 60


def backup_cadence(recent: list | None) -> int:
    """Обычный интервал между бэкапами репозитория по временам последних снапшотов, секунды;
    0 - ритм не ясен (мало снапшотов или интервалы не сходятся).

    Только голова списка: старше политика хранения уже проредила снапшоты (на серверах после
    7 суточных идут 72, 168, 480 часов). Прореживание склеивает соседние интервалы, поэтому
    ритм - самые свежие интервалы, пока они сходятся с самым новым, и таких нужно хотя бы
    два. Иначе ритм не ясен: при keep-daily 2 за одним суточным идут недельные, и два
    недельных подряд выдали бы ритм "раз в неделю", а бэкап с ним заметили бы позже, чем по
    прежним 3 дням. Ручной бэкап посреди дня тоже делает ритм неясным, пока не уйдет вглубь."""
    ts = sorted({int(t) for t in (recent or []) if isinstance(t, (int, float)) and t > 0}, reverse=True)
    runs: list[int] = []
    for t in ts:
        if not runs or runs[-1] - t > _RUN_GAP:
            runs.append(t)
    iv = [runs[i] - runs[i + 1] for i in range(min(len(runs) - 1, 6))]
    head: list[int] = []
    for x in iv:
        if head and not 0.8 * iv[0] <= x <= 1.25 * iv[0]:
            break
        head.append(x)
    if len(head) < 2:
        return 0
    return sorted(head)[(len(head) - 1) // 2]


def _every(sec: int) -> str:
    """Интервал бэкапов словами: раз в сутки, раз в 6 ч, раз в неделю."""
    h = sec / 3600
    if abs(h - 24) <= 2:
        return "раз в сутки"
    if abs(h - 168) <= 12:
        return "раз в неделю"
    if abs(h - 1) <= 0.25:
        return "раз в час"
    if h < 48:
        return f"раз в {round(h)} ч"
    return f"раз в {round(h / 24)} дн"


def _ago(sec: float) -> str:
    return f"{int(sec // 3600)} ч" if sec < 2 * 86400 else f"{int(sec // 86400)} дн"


# Архив репозиториев и старых групп снапшотов (Server.backup_repo_archive): сервера больше нет,
# бэкапить больше не нужно, разовый бэкап, проект заморожен. Данные остаются как есть: чистка
# по политике у репозитория без новых снапшотов ничего не удаляет (keep-daily считает дни с
# бэкапами, а не календарные), а удалить группу можно только руками. Архив не устаревает, не
# попадает в ротацию и в "клиенты без агента", но проверка целостности и висячий лок
# остаются: архив должен читаться. Ключ - имя репозитория или "репозиторий|хост" для группы.
def archived_repo(archive: dict | None, name: str) -> bool:
    return bool(archive) and name in archive


def archived_hosts(archive: dict | None, repo: str) -> set[str]:
    """Хосты групп репозитория, убранных в архив."""
    pre = repo + "|"
    return {k[len(pre):] for k in (archive or {}) if k.startswith(pre)}


# Группы снапшотов пишет prune-скрипт раз в сутки. Старше трех суток он, похоже, не
# запускается, и по таким группам уже не судим.
_GROUPS_MAX_AGE = 3 * 86400


def repo_groups(extra: dict | None, name: str, now_ts: float) -> list[dict]:
    """Группы снапшотов репозитория (хост и теги) от helper'а 0.28, свежие; иначе пусто."""
    o = ((extra or {}).get("repos") or {}).get(name)
    g = o.get("groups") if isinstance(o, dict) else None
    if not isinstance(g, dict) or now_ts - float(g.get("ts") or 0) > _GROUPS_MAX_AGE:
        return []
    return [x for x in g.get("groups") or [] if isinstance(x, dict)]


def _live_view(r: dict, extra: dict | None, archive: dict | None, now_ts: float) -> tuple[int, int]:
    """(снапшотов, старейший) репозитория без групп, убранных в архив. Без архивных групп или
    без сведений о группах - как есть."""
    snaps = int(r.get("snapshots") or 0)
    oldest = int(r.get("oldest_snapshot") or 0)
    name = r.get("name") or ""
    hosts = archived_hosts(archive, name)
    groups = repo_groups(extra, name, now_ts) if hosts else []
    if not groups:
        return snaps, oldest
    live = [g for g in groups if (g.get("host") or "") not in hosts]
    gone = sum(int(g.get("n") or 0) for g in groups if (g.get("host") or "") in hosts)
    firsts = [int(g.get("first") or 0) for g in live if int(g.get("first") or 0) > 0]
    return max(snaps - gone, 0), (min(firsts) if firsts else oldest)


# Висячий лок держит prune, forget и check. Чистка Kervax, клиенты Kervax и ansible снимают
# висячие локи сами (restic unlock перед работой), поэтому лок моложе суток почти всегда
# уходит без человека. Старше суток - значит, ежедневная чистка на нем уже споткнулась и
# дальше будет так же. Старый общий скрипт /etc/systemd-rest.conf unlock не делает, и на
# backup-a чистка app-a так простояла 23 дня.
_BACKUP_LOCK_ALERT_SECONDS = 86400


def lock_age(r: dict, now_ts: float) -> float:
    """Сколько секунд репозиторий держит висячий лок, 0 - лока нет, он живой или время
    неизвестно. helper 0.23 отдает в lock_ts самый старый висячий лок, так что это время,
    с которого репозиторий заблокирован, даже если рядом идет бэкап."""
    ts = r.get("lock_ts") or 0
    if not r.get("locked") or ts <= 0:
        return 0.0
    age = now_ts - ts
    return age if age > _BACKUP_LOCK_STUCK_SECONDS else 0.0


def long_locked_repos(bs: dict, muted: set, now: datetime) -> list[tuple[str, int]]:
    """Не заглушенные репозитории с висячим локом дольше суток: (имя, полных суток)."""
    out = []
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        age = lock_age(r, now.timestamp())
        if name and name not in muted and age > _BACKUP_LOCK_ALERT_SECONDS:
            out.append((name, int(age // 86400)))
    return sorted(out)


# Недельная проверка целостности (restic check в нашем prune-скрипте, helper 0.25): старый общий
# скрипт проверял каждый день и слал ошибку в Telegram, переезд на свой скрипт не должен ее
# терять. Результат старше двух недель не считаем: скрипт с проверкой, похоже, больше не
# запускается, и про это скажет ротация.
_BACKUP_CHECK_FRESH = 14 * 86400


def failed_checks(bs: dict, extra: dict, muted: set, now: datetime) -> list[str]:
    """Репозитории, у которых последняя недельная проверка не прошла. Без заглушенных."""
    out = []
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        o = ((extra or {}).get("repos") or {}).get(name)
        if not name or name in muted or not isinstance(o, dict):
            continue
        ts = int(o.get("check_ts") or 0)
        if int(o.get("check_ok") if o.get("check_ok") is not None else -1) == 0 \
                and ts > 0 and now.timestamp() - ts < _BACKUP_CHECK_FRESH:
            out.append(name)
    return sorted(out)


def panel_server_names(servers) -> set[str]:
    """Имена включенных нод панели для сверки с репозиториями бэкап-серверов: имя сервера,
    hostname и его короткая часть до точки. Репозиторий назван по клиенту (так его заводят и
    ansible-роль, и панель), поэтому совпадение по имени надежное."""
    out: set[str] = set()
    for s in servers:
        if not getattr(s, "enabled", True):
            continue
        # и имя машины из отчета агента: им ansible-роль называет репозиторий клиента
        for n in (s.name, s.hostname, (s.last_report or {}).get("hostname")):
            n = (n or "").strip().lower()
            if n:
                out.add(n)
                out.add(n.split(".")[0])
    return out


def backup_unmonitored(
    bs: dict, muted: set, names: set[str], now: datetime, archive: dict | None = None,
) -> list[str]:
    """Репозитории со свежими бэкапами, чьих клиентов нет в панели. Если такой бэкап сломается,
    панель узнает только через 3 дня, когда репозиторий устареет, а агент на самой ноде сказал
    бы в тот же день. Так прошли 23 дня у app-a. Заглушенные и устаревшие (клиента,
    похоже, уже нет) не считаем."""
    out = []
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        last = r.get("last_activity") or 0
        if not name or name in muted or archived_repo(archive, name) or not last                 or now.timestamp() - last > _BACKUP_REPO_STALE_SECONDS:
            continue
        if name.lower() not in names:
            out.append(name)
    return sorted(out)


# Блок helper'а backupserver-setup 0.23 (report.d/backup-server.json): кто чистит каждый
# репозиторий (свой prune-скрипт, старый общий скрипт, никто) и когда из него последний раз
# пропадали снапшоты. Пишется раз в минуту, старше 15 минут - helper встал, судить не по чему.
BSRV_EXTRA_KEY = "backup-server"
_BSRV_EXTRA_MAX_AGE = 15 * 60


def bsrv_extra(rep: dict) -> dict:
    """Свежий блок backup-server из отчета или пустой словарь."""
    block = ((rep.get("extras") or {}).get(BSRV_EXTRA_KEY)) or {}
    if not isinstance(block, dict) or block.get("v") != 1:
        return {}
    ts = float(block.get("ts") or 0)
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > _BSRV_EXTRA_MAX_AGE:
        return {}
    return block


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def bsrv_restic_old(rep: dict) -> str:
    """Версия restic сервера бэкапов, если она старше целевой, иначе "".

    Обе версии из блока helper'а (backupserver-setup 0.27+): он знает, до какой версии
    обновляет restic, и панели не нужно держать свою копию этого числа. restic-update
    берет самую старую из тех, что сервер запускает, ее же helper и сообщает."""
    b = bsrv_extra(rep)
    cur, want = str(b.get("restic") or ""), str(b.get("restic_target") or "")
    if not _ver_tuple(cur) or not _ver_tuple(want):
        return ""
    return cur if _ver_tuple(cur) < _ver_tuple(want) else ""


def _muted(key: str, level: int, mutes: set) -> bool:
    """Заглушён ли алерт. Кроме простого «весь тип» (`disk`) поддерживаем УРОВЕНЬ:
    `disk@1` = молчать про предупреждения, но алертить проблему и критику. Нужно там,
    где у порога несколько ступеней: диск на 86% при пороге предупреждения 85 шумит
    каждый день, а вот 95% пропускать нельзя. Формат `<тип>@<макс. заглушённый уровень>`.
    """
    if key in mutes:
        return True
    for m in mutes:
        if not m.startswith(key + "@"):
            continue
        try:
            if level <= int(m.split("@", 1)[1]):
                return True
        except ValueError:
            continue  # мусорный ключ игнорируем, а не роняем сбор алертов
    return False


# Подавление «мигания» связи. Нода на перегруженном канале может уходить и
# возвращаться каждые несколько минут: каждое переключение — два сообщения, и лента
# превращается в шум, за которым не видно настоящих аварий. Считаем переключения в
# скользящем окне; после порога шлём ОДНО «связь нестабильна» и молчим, пока не
# успокоится (тогда — одно «снова стабильна»).
_FLAP_WINDOW = 30 * 60  # окно наблюдения, с
_FLAP_LIMIT = 4         # столько переключений в окне = «мигает»


# Сколько дней снапшоты вправе жить по политике: суточные + недельные + месячные.
# Плюс запас: prune ходит раз в сутки, а месяц бывает длиннее 30 дней.
_ROT_GRACE_DAYS = 10


def rotation_max_age_days(repo: dict) -> int:
    """Верхняя граница возраста САМОГО СТАРОГО снапшота по политике репозитория."""
    d = int(repo.get("keep_daily") or 0)
    w = int(repo.get("keep_weekly") or 0)
    m = int(repo.get("keep_monthly") or 0)
    span = d + w * 7 + m * 31
    if span <= 0:
        return 0  # политики нет — судить не о чем
    return span + _ROT_GRACE_DAYS


# Второй, БЫСТРЫЙ признак. Возраст старейшего снапшота честен, но нетороплив: при
# keep-monthly 6 он терпит 231 день, а диск активного бэкап-сервера кончается за
# недели. Прямой признак виден на вторые сутки — снапшотов больше, чем оставляет
# политика, и при этом ни один прогон ничего не удаляет.
#
# Почему нужны ОБА условия. Переполнение само по себе законно: forget группирует по
# host+tags и применяет политику к каждой группе, так что репозиторий с двумя
# клиентами держит два комплекта. Но в такой группе ротация ЖИВАЯ — прогоны что-то
# удаляют. Мёртвая ротация даёт ноль удалений подряд, сколько бы групп ни было, и
# число групп панели знать не нужно.
_ROT_DEAD_DAYS = 3


def rotation_policy_max(repo: dict) -> int:
    """Сколько снапшотов оставляет политика ОДНОЙ группы. 0 = политики нет."""
    return sum(
        int(repo.get(f"keep_{k}") or 0) for k in ("last", "daily", "weekly", "monthly")
    )


def rotation_overflow(
    bsrv: dict, seen: dict, now: datetime, extra: dict | None = None, archive: dict | None = None,
) -> tuple[list[str], dict]:
    """Репозитории, где ротация не отрабатывает: переполнение + ноль удалений.

    seen — карта «репозиторий → когда это заметили впервые» из состояния сервера.
    Возвращает (список для алерта, новая карта). Пока не выдержан _ROT_DEAD_DAYS,
    репозиторий копится в карте, но в алерт не идёт: единичный лок на репозитории
    или прогон, которому нечего было удалять, — обычное дело.

    extra - блок helper'а backup-server (bsrv_extra). Метрики удалений пишут только наши
    prune-скрипты, а репозиторий, который чистит старый общий скрипт или никто, сюда
    раньше не попадал вовсе. Для него удаления видит helper по пропавшим файлам
    снапшотов, и отсчет идет от последнего удаления (или от начала наблюдения).

    archive - архив репозиториев и групп (Server.backup_repo_archive): репозиторий в архиве не
    проверяем, снапшоты групп в архиве не считаем.
    """
    fresh: dict = {}
    out: list[str] = []
    observed = (extra or {}).get("repos") or {}
    for r in bsrv.get("repos") or []:
        name = r.get("name") or ""
        if archived_repo(archive, name):
            continue
        snaps, _oldest = _live_view(r, extra, archive, now.timestamp())
        limit = rotation_policy_max(r)
        removed = int(r.get("rotation_removed") if r.get("rotation_removed") is not None else -1)
        if not name or limit <= 0 or snaps <= limit:
            continue  # нет политики или все в пределах
        last = r.get("last_activity") or 0
        if last > 0 and now.timestamp() - last > _BACKUP_REPO_STALE_SECONDS:
            # новых снапшотов нет, значит и удалять нечего: это не ротация встала, а бэкап
            # перестал приходить, про это алерт backup_repo
            continue
        if removed < 0:
            o = observed.get(name)
            mark = 0
            if isinstance(o, dict):
                mark = max(int(o.get("removed_ts") or 0), int(o.get("seen_since") or 0))
            if mark <= 0:
                continue  # ни метрик, ни наблюдений helper'а (он старше 0.23)
            idle = (now.timestamp() - mark) / 86400
            if idle >= _ROT_DEAD_DAYS:
                out.append(f"{name} ({snaps} снапшотов при политике {limit}, удалений нет {int(idle)} дн)")
            continue
        if removed != 0:
            continue  # ротация что-то убирает
        since = seen.get(name) or now.isoformat()
        fresh[name] = since
        started = _parse_iso(since)
        held = (now - started).total_seconds() / 86400 if started else 0
        if held >= _ROT_DEAD_DAYS:
            out.append(f"{name} ({snaps} снапшотов при политике {limit}, удалений нет)")
    return sorted(out), fresh


def rotation_stale_repos(
    bsrv: dict, now: datetime, extra: dict | None = None, archive: dict | None = None,
) -> list[str]:
    """Репозитории, где старейший снапшот пережил собственную политику хранения.

    Главный признак мёртвой ротации, инвариантный к причине: неважно, сломалась ли
    группировка forget, упал prune или снят cron — старьё просто перестаёт исчезать.
    Именно этого сигнала не хватало, когда 17 дней все было зеленым.

    extra - блок helper'а backup-server: по нему видно, что чистка при этом идет.
    archive - архив: репозиторий в архиве не проверяем, группы в архиве не считаем (старая
    группа, которую решили хранить, больше не выглядит встающей ротацией)."""
    out: list[str] = []
    observed = (extra or {}).get("repos") or {}
    for r in bsrv.get("repos") or []:
        if archived_repo(archive, r.get("name") or ""):
            continue
        snaps, oldest = _live_view(r, extra, archive, now.timestamp())
        limit = rotation_max_age_days(r)
        if oldest <= 0 or limit <= 0:
            continue  # нет метрики (старый helper) или нет политики — молчим
        # Снапшотов не больше, чем политика вообще может оставить, - значит, их она и
        # оставила: keep-daily считает дни, в которые были бэкапы, а не календарные. У
        # клиента, который бэкапится раз в месяц, 7 снапшотов законно держатся больше
        # года (app-d: 458 дней, и все семь на своем месте).
        if snaps <= rotation_policy_max(r):
            continue
        age = (now.timestamp() - oldest) / 86400
        if age > limit:
            # чистка при этом идет - тогда старье почти наверняка другая группа: forget
            # группирует по хосту и тегам, и последние снапшоты старой группы держит вечно.
            # helper 0.28 знает группы, тогда называем её прямо.
            name = r.get("name") or ""
            old = old_groups(extra, archive, name, now.timestamp())
            if old:
                # причина словами вместо "233 дн. > 231": чья это история и что ее держит
                out.append(f"{name or '?'} ({old_group_text(old, repo_groups(extra, name, now.timestamp()))})")
                continue
            if rotation_alive(r, observed.get(name), now):
                hint = ", похоже на старую группу: менялись хост или пути"
            else:
                hint = ""
            out.append(f"{name or '?'} (старейшему снапшоту {int(age)} дн. при политике до {limit}{hint})")
    return sorted(out)


def group_rotation(items: list[str]) -> dict[str, list[str]]:
    """Строки rotation_stale_repos и rotation_overflow ("repo-a (причина)") по репозиториям:
    имя -> причины, по имени."""
    by: dict[str, list[str]] = {}
    for x in items:
        name = x.split(" (")[0]
        why = x[len(name) + 2:-1] if x.endswith(")") and len(x) > len(name) + 2 else ""
        got = by.setdefault(name, [])
        if why and why not in got:
            got.append(why)
    return dict(sorted(by.items()))


def merge_rotation(items: list[str]) -> list[str]:
    """Одна строка на репозиторий: "repo-a (причина 1, причина 2)". Старье пережило политику и
    ротация ничего не удаляет - обычно одна и та же беда (чистку никто не запускает), а в "Чистке",
    на главной и в алерте она шла двумя строками про один репозиторий."""
    return [f"{n} ({', '.join(w)})" if w else n for n, w in group_rotation(items).items()]


# Чистка репозитория (prune-скрипт helper'а) не работает: нет ни ротации, ни недельной проверки
# целостности, а алерт ротации скажет об этом только через несколько дней и не о причине. Агент
# отдает итог последнего прогона: rotation_ok 0 - команды упали, а rotation_removed -1 при этом
# значит, что скрипт не открыл репозиторий (обычно не тот пароль в env). helper 0.30 отдает и
# строку ошибки (prune_err). Итогу старше трех суток не верим: тогда чистка не запускается.
_PRUNE_FRESH = 3 * 86400


def _prune_reason(err: str) -> str:
    """Суть ошибки restic из строки лога: с "Fatal:", иначе строка как есть. Строка старого
    helper'а без причины ("repo not accessible at <путь>") ничего не добавляет."""
    err = (err or "").strip()
    if "Fatal:" in err:
        return err[err.index("Fatal:"):][:160]
    if err.startswith("ERROR: repo not accessible"):
        return ""
    return err[:160]


def prune_problems(
    bs: dict, extra: dict | None, now: datetime, muted: set | None = None, archive: dict | None = None,
) -> dict[str, tuple[str, str]]:
    """Репозитории, у которых не работает чистка: имя -> (вид, причина словами). Виды: access -
    не открывает репозиторий, failed - команды упали, skipped - пропущена (пустая политика),
    idle - не запускается. Без заглушенных и архива."""
    out: dict[str, tuple[str, str]] = {}
    nowts = now.timestamp()
    obs = (extra or {}).get("repos") or {}
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        if not name or name in (muted or set()) or archived_repo(archive, name):
            continue
        o = obs.get(name) if isinstance(obs.get(name), dict) else {}
        ts = int(r.get("rotation_ts") or 0)
        ok = int(r.get("rotation_ok") if r.get("rotation_ok") is not None else -1)
        removed = int(r.get("rotation_removed") if r.get("rotation_removed") is not None else -1)
        err = str(o.get("prune_err") or "")
        fresh = ts > 0 and nowts - ts < _PRUNE_FRESH
        if fresh and err.startswith("SAFETY:"):
            out[name] = ("skipped", "чистка пропущена: политика хранения пустая, все keep-* = 0")
        elif fresh and ok == 0 and removed < 0:
            out[name] = ("access", "чистка не открывает репозиторий: "
                         + (_prune_reason(err) or "не подходит пароль в env или env нет"))
        elif fresh and ok == 0:
            why = _prune_reason(err)
            out[name] = ("failed", f"чистка падает: {why}" if why else "чистка падает, подробности в логе prune-скрипта")
        elif o.get("cleaner") == "script" and ts > 0 and not fresh:
            out[name] = ("idle", f"чистка не запускается {int((nowts - ts) // 86400)} дн")
        elif o.get("cleaner") == "script" and ts <= 0 and int(o.get("prune_mtime") or 0) > 0 \
                and nowts - int(o.get("prune_mtime") or 0) >= _PRUNE_FRESH:
            # от того, когда скрипт сгенерирован (helper 0.30), а не от начала наблюдения: только
            # что перенесенный со старого скрипта репозиторий просто ждет ночи
            out[name] = ("idle", "чистка ни разу не запускалась")
    return out


# Упавшую чистку (failed) алертим, только если упал и следующий прогон: единичный сбой, например
# лок, который не дождался 20 минут, проходит сам. Не открывает репозиторий или не запускается -
# сразу: это само не пройдет.
_PRUNE_FAIL_HOLD = 26 * 3600


# Группа старая, если в нее не бэкапились неделю: столько же ждет и forget-group, прежде чем
# согласиться её удалить.
_OLD_GROUP_SECONDS = 7 * 86400


def old_groups(extra: dict | None, archive: dict | None, name: str, now_ts: float) -> list[dict]:
    """Старые группы репозитория, не убранные в архив, свежие сначала. Группа, в которую
    бэкапились последней, старой не бывает, даже если бэкапы встали: это сам клиент."""
    groups = repo_groups(extra, name, now_ts)
    if len(groups) < 2:
        return []
    hosts = archived_hosts(archive, name)
    newest = max(int(g.get("last") or 0) for g in groups)
    return [g for g in groups
            if int(g.get("last") or 0) < newest and now_ts - int(g.get("last") or 0) > _OLD_GROUP_SECONDS
            and (g.get("host") or "") not in hosts]


# Переименование, а не ушедший сервер: самая свежая группа началась не позже чем через неделю
# после последнего снапшота старой, с теми же путями и тегами (репозиторий у клиента свой)
_RENAME_GAP = 7 * 86400


def renamed_to(groups: list[dict], g: dict) -> str:
    """Под каким именем бэкапится сервер, если старая группа g - его прежнее имя; иначе пусто."""
    if not groups:
        return ""
    cur = max(groups, key=lambda x: int(x.get("last") or 0))
    if (cur.get("host") or "") in ("", g.get("host") or ""):
        return ""
    gap = int(cur.get("first") or 0) - int(g.get("last") or 0)
    same = (sorted(cur.get("paths") or []) == sorted(g.get("paths") or [])
            and sorted(cur.get("tags") or []) == sorted(g.get("tags") or []))
    return str(cur.get("host")) if same and abs(gap) <= _RENAME_GAP else ""


def old_group_text(old: list[dict], groups: list[dict]) -> str:
    """Чье старье держится, по первой старой группе: "сервер переименовали из X: 15 снапшотов под
    старым именем, по 19.07.2026" или "группа X больше не бэкапится: 15 снапшотов, по 19.07.2026"."""
    g = old[0]
    host = g.get("host") or "?"
    n = int(g.get("n") or 0)
    last = datetime.fromtimestamp(int(g.get("last") or 0), timezone.utc).strftime("%d.%m.%Y")
    snaps = f"{n} {_plural(n, 'снапшот', 'снапшота', 'снапшотов')}"
    if renamed_to(groups, g):
        text = f"сервер переименовали из {host}: {snaps} под старым именем, по {last}"
    else:
        text = f"группа {host} больше не бэкапится: {snaps}, по {last}"
    if len(old) > 1:
        text += f", и еще старых групп: {len(old) - 1}"
    return text


def rotation_reasons(s: Server, now: datetime) -> dict[str, list[str]]:
    """Что сейчас сказал бы алерт backup_rotation, по репозиториям: имя -> причины (старье
    пережило политику, ротация ничего не удаляет). Заглушенные репозитории не в счет, как в алерте."""
    rep = s.last_report or {}
    bs = rep.get("backup_server") or {}
    if not bs.get("present"):
        return {}
    ext = bsrv_extra(rep)
    arch = getattr(s, "backup_repo_archive", None) or {}
    over, _seen = rotation_overflow(bs, dict((s.alert_state or {}).get("rotation_over") or {}), now, ext, arch)
    muted = set(s.backup_repo_mutes or [])
    return group_rotation([x for x in rotation_stale_repos(bs, now, ext, arch) + over
                           if x.split(" (")[0] not in muted])


def backup_rotation_items(s: Server, now: datetime) -> list[str]:
    """То же одной строкой на репозиторий ("repo-a (причина 1, причина 2)"): для "Что сломано" на
    главной и замечаний карточки."""
    return [f"{n} ({', '.join(w)})" if w else n for n, w in rotation_reasons(s, now).items()]


def rotation_alive(r: dict, observed: dict | None, now: datetime) -> bool:
    """Удаляет ли что-то чистка: по метрике своего prune-скрипта (снято за последний прогон)
    или по удалениям, которые видел helper за последние _ROT_DEAD_DAYS суток."""
    if int(r.get("rotation_removed") or 0) > 0:
        return True
    ts = int((observed or {}).get("removed_ts") or 0) if isinstance(observed, dict) else 0
    return ts > 0 and now.timestamp() - ts < _ROT_DEAD_DAYS * 86400


# --- «почему» в тексте алерта -------------------------------------------------
# «CPU 92%» не отвечает на единственный нужный вопрос: из-за чего. 18.09.2026 на
# stand-a рекламная сеть залила 2.3 млн переходов в сутки вместо обычных шести
# тысяч. Пришёл алерт по CPU, разбор занял вечер, хотя всё нужное панель уже
# знала: топ процессов лежит в том же отчёте, трафик и соединения - в минутной
# истории. Теперь это дописывается в сам алерт ({cause} в шаблоне).
_CAUSE_MIN_CPU = 5.0        # % одного ядра: процессы мельче не упоминаем
_CAUSE_TOP_N = 3
_CAUSE_RATIO = 3.0          # во столько раз выше нормы = стоит сказать
_BASE_DAYS = 7              # по скольким суткам считаем норму того же времени
_BASE_HALF = timedelta(minutes=30)  # окно вокруг того же времени в каждом из тех суток
_BASE_MIN_SAMPLES = 20      # меньше замеров - истории мало, молчим
# Пороги «это настоящий поток, а не проснувшаяся из нуля нода»: ночью на простое
# базой бывают байты в секунду, и тогда любой бэкап дал бы «трафик x900».
_SURGE_MIN_NET = 1_000_000  # байт/сек
_SURGE_MIN_CONN = 200
_SURGE_MIN_RPM = 600        # запросов в минуту: 10 в секунду это уже поток


def _fmt_size(b: float) -> str:
    return f"{b / 1024 ** 3:.1f} ГБ" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.0f} МБ"


# Разбор места helper'а diskusage-setup: блок есть, пока раздел заполняется (от 75%),
# helper обновляет его раз в 10 минут. Старше двух часов - helper встал, не показываем.
_DISK_USAGE_MAX_AGE = 2 * 3600
# "Без риска освобождается" упоминаем в алерте, только если это заметная величина
_DISK_SAFE_MIN = 256 * 1024 ** 2


def disk_usage_block(rep: dict) -> dict | None:
    """Блок "на что ушло место" из отчета (report.d/disk-usage.json, агент отдает как есть).
    Возраст меряем часами самой ноды, как у блока веба: при сдвинутых часах живые данные
    иначе пропадали бы."""
    block = ((rep.get("extras") or {}).get("disk-usage")) or {}
    if not isinstance(block, dict) or not isinstance(block.get("fs"), list) or not block["fs"]:
        return None
    ts = float(block.get("ts") or 0)
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > _DISK_USAGE_MAX_AGE:
        return None
    return block


def disk_top_dirs(fs: dict, n: int = 2) -> list[tuple[str, float]]:
    """Самые большие каталоги раздела без матрешки: /var не называем, если почти все в нем -
    это /var/lib/docker. Каталог пропускаем, когда его потомок из списка держит больше 60%
    его объема, - тогда назван будет потомок."""
    top = [(str(t.get("path") or ""), float(t.get("bytes") or 0))
           for t in fs.get("top") or [] if isinstance(t, dict)]
    keep = []
    for p, b in top:
        if not p or b <= 0:
            continue
        pre = p.rstrip("/") + "/"
        if any(q.startswith(pre) and qb >= 0.6 * b for q, qb in top):
            continue
        keep.append((p, b))
    keep.sort(key=lambda x: -x[1])
    return keep[:n]


def disk_cause(rep: dict, forecast: dict | None = None, mount: str | None = None,
               now: datetime | None = None) -> str:
    """Хвост к дисковому алерту: когда при нынешнем росте раздел заполнится (прогноз
    планировщика), на что ушло место и сколько освобождается без риска на самом
    заполненном разделе. Пусто без прогноза и блока helper'а - тогда алерт как раньше."""
    parts = []
    eta = dfc.mount_eta(forecast, mount, "space", now or datetime.now(timezone.utc)) if mount else None
    if eta is not None:
        parts.append(f"заполнится примерно {dfc.eta_text(eta)}")
    block = disk_usage_block(rep)
    fss = [f for f in (block or {}).get("fs") or [] if isinstance(f, dict)]
    if fss:
        worst = max(fss, key=lambda f: float(f.get("pct") or 0))
        dirs = disk_top_dirs(worst)
        if dirs:
            parts.append("больше всего места: " + ", ".join(f"{p} {_fmt_size(b)}" for p, b in dirs))
        safe = sum(float(i.get("free") or 0) for i in block.get("items") or []
                   if isinstance(i, dict) and i.get("level") == "safe"
                   and i.get("mount") == worst.get("mount"))
        if safe >= _DISK_SAFE_MIN:
            parts.append(f"без риска освобождается ~{_fmt_size(safe)}")
    return " - " + "; ".join(parts) if parts else ""


def _fmt_files(n: float) -> str:
    return f"{n / 1e6:.1f} млн" if n >= 1e6 else f"{n / 1e3:.0f} тыс." if n >= 1e3 else str(int(n))


def inode_cause(rep: dict, forecast: dict | None, mount: str | None, now: datetime) -> str:
    """Хвост к алерту по inode: где скопились файлы (дерево helper'а diskusage-setup 0.5)
    и когда inode кончатся при нынешнем росте."""
    parts = []
    eta = dfc.mount_eta(forecast, mount, "inode", now) if mount else None
    if eta is not None:
        parts.append(f"кончатся примерно {dfc.eta_text(eta)}")
    block = disk_usage_block(rep) or {}
    for fs in block.get("fs") or []:
        if not isinstance(fs, dict) or fs.get("mount") != mount:
            continue
        top = [{"path": t.get("path"), "bytes": t.get("files")} for t in fs.get("itop") or []
               if isinstance(t, dict)]
        dirs = disk_top_dirs({"top": top})
        if dirs:
            parts.append("больше всего файлов: " + ", ".join(f"{p} {_fmt_files(n)}" for p, n in dirs))
    return " - " + "; ".join(parts) if parts else ""


# Здоровье физических дисков (helper diskhealth-setup): блок обновляется раз в 2 минуты.
# Старше получаса - helper встал, и судить о дисках не по чему: живой развалившийся RAID
# не должен "выздоравливать" оттого, что отчеты перестали приходить.
_DISK_HEALTH_MAX_AGE = 30 * 60
# Ошибок ввода-вывода в журнале ядра за 10 минут (окно helper'а), меньше - не повод:
# одиночные строки бывают при загрузке и у исправного железа
_DISK_IO_MIN = 10
# Пачки ошибок в пределах часа - одна история, а не алерт и отбой на каждую пачку
_DISK_IO_HOLD = timedelta(hours=1)
_DISK_WEAR_WARN = 90
# Прогноз заполнения: за сколько часов до конца места или inode предупреждать и бить тревогу.
# Тот же срок, с которого пункт есть в "Что сломано": раньше там он висел с 10 дней, а алерт
# приходил за трое суток, и было непонятно, почему "сломано", а тишина.
_FORECAST_WARN_H = 5 * 24
_FORECAST_CRIT_H = 24
# Счетчики, рост которых за сутки значит, что диск умирает сейчас (статичное число,
# оставшееся с давних времен, - не новость)
_DISK_GROW_LABEL = {
    "realloc": "переназначенные сектора",
    "pending": "нечитаемые сектора",
    "uncorr": "неисправимые ошибки",
    "media": "ошибки носителя",
}


def disk_health_block(rep: dict) -> dict | None:
    """Блок здоровья дисков из отчета (report.d/disk-health.json, агент отдает как есть).
    Возраст - по часам самой ноды, как у остальных блоков helper'ов."""
    block = ((rep.get("extras") or {}).get("disk-health")) or {}
    if not isinstance(block, dict) or block.get("v") != 1:
        return None
    ts = float(block.get("ts") or 0)
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > _DISK_HEALTH_MAX_AGE:
        return None
    return block


def _disk_title(d: dict) -> str:
    """Имя диска с моделью и серийником: nvme1n1 (Samsung SSD 990 PRO 2TB, S7DNNU0Y719359D).
    В ЦОД диск меняют по серийнику, а имя устройства после перезагрузки может уехать на
    соседний."""
    extra = ", ".join(x for x in (str(d.get("model") or "").strip(),
                                  str(d.get("serial") or "").strip()) if x)
    dev = str(d.get("dev") or "?")
    return f"{dev} ({extra})" if extra else dev


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def disk_health_problems(block: dict, short: bool = False, wear_hist: dict | None = None,
                         writers: list | None = None) -> list[tuple[int, str, str]]:
    """Поломки дисков: (уровень, ключ, текст), серьезные первыми. Ключ стабилен между
    отчетами: по нему алерт понимает, что сломалось НОВОЕ (второй диск при уже
    развалившемся RAID), а не то, о чем уже сказано. Склеенные в одну строку массивы
    дают по записи на массив без текста и одну запись с текстом без ключа: досинхронизи-
    ровавшийся первым md42 не должен выглядеть новой поломкой md43. Ошибки журнала ядра
    сюда не входят, у них своя выдержка (см. _server_conditions). short - диск только по
    имени, без модели и серийника: для сводки на главной. wear_hist - servers.disk_wear:
    диск дойдет до 90% износа меньше чем через два месяца (app/disk_wear.py). writers -
    disk_writers агента: к такому износу строка, кто больше всех пишет."""
    out: list[tuple[int, str, str]] = []
    soon: list[str] = []
    # RAID с одинаковой бедой склеиваем: оба массива на двух дисках теряют разделы одного
    # и того же умершего диска, и "md42: 1 из 2; md43: 1 из 2" - это одна новость
    degraded: dict[str, list[str]] = {}
    for r in block.get("raid") or []:
        if not isinstance(r, dict):
            continue
        dev = str(r.get("dev") or "md?")
        tot, act = _int(r.get("total")), _int(r.get("active"))
        failed = str(r.get("failed") or "").strip()
        if r.get("state") == "inactive":
            out.append((3, f"raid:{dev}", f"RAID {dev} не собран (inactive)"))
        elif tot and act < tot:
            tail = (f" ({r['level']})" if r.get("level") else "") + f": работает {act} из {tot} дисков"
            if failed:
                tail += f", сбойный {failed}"
            m = re.match(r"(recovery|resync|reshape)=([\d.]+)%", str(r.get("sync") or ""))
            if m:
                tail += f", идет восстановление {m.group(2)}%"
            degraded.setdefault(tail, []).append(dev)
        elif failed:
            out.append((2, f"raidf:{dev}",
                        f"RAID {dev}: выпал {failed}, работу взял запасной диск"))
    for tail, devs in degraded.items():
        for dev in devs:
            out.append((3, f"raid:{dev}", ""))
        out.append((3, "", f"RAID {', '.join(devs)}{tail}"))

    for d in block.get("disks") or []:
        if not isinstance(d, dict):
            continue
        name = str(d.get("dev") or "?") if short else _disk_title(d)
        ser = str(d.get("serial") or d.get("dev") or "?")
        err = str(d.get("err") or "")
        if err == "virtual":  # диск ВМ (helper 0.2): SMART нет, здоровье железа видит хостер
            continue
        if d.get("ok") is False:
            if err == "dead":
                why = "не определяется (размер 0)"
            elif err.startswith("no_namespace"):
                why = "не определяется: контроллер есть, диска нет"
            elif err.startswith("state_"):
                why = f"отключен ядром ({err[6:]})"
            else:
                why = "не отвечает"
            out.append((3, f"dead:{ser}", f"диск {name} {why}"))
            continue
        health = str(d.get("health") or "").upper()
        wear = d.get("wear") if isinstance(d.get("wear"), (int, float)) else None
        failing = "FAIL" in health or d.get("failing") is True
        if failing:
            # SSD, выработавший ресурс, SMART тоже считает неисправным, но такой диск обычно
            # еще работает: менять надо, будить ночью - нет
            worn = wear is not None and wear >= 100
            out.append((2 if worn else 3, f"smart:{ser}",
                        f"диск {name}: SMART считает диск неисправным"
                        + (f", ресурс выработан (износ {wear}%)" if worn else "")))
        crit = str(d.get("crit") or "")
        try:
            cw = int(crit, 16) if crit else 0
        except ValueError:
            cw = 0
        if cw & ~0x02:  # запас, надежность, только чтение, резервная память
            out.append((3, f"crit:{ser}", f"диск {name}: NVMe сообщает о деградации ({crit})"))
        elif cw & 0x02:
            out.append((2, f"hot:{ser}", f"диск {name}: NVMe сообщает о перегреве"))
        grow = d.get("grow") if isinstance(d.get("grow"), dict) else {}
        bad = [f"{lbl} +{_int(grow.get(k))}" for k, lbl in _DISK_GROW_LABEL.items() if _int(grow.get(k)) > 0]
        if bad:
            out.append((2, f"grow:{ser}", f"диск {name}: за сутки выросли ошибки - " + ", ".join(bad)))
        if _int(grow.get("crc")) > 0:
            out.append((1, f"crc:{ser}", f"диск {name}: за сутки +{_int(grow.get('crc'))} ошибок "
                                         "передачи (CRC), проверьте кабель или разъем"))
        if wear is not None and wear >= _DISK_WEAR_WARN and not failing:
            out.append((1, f"wear:{ser}", f"диск {name}: износ {wear}%, пора планировать замену"))
        rec = wear_hist.get(ser) if isinstance(wear_hist, dict) else None
        if (isinstance(rec, dict) and rec.get("soon") and rec.get("d90") and not failing
                and wear is not None and wear < _DISK_WEAR_WARN):
            soon.append(name)
            out.append((1, f"wearsoon:{ser}", disk_wear.soon_text(name, rec, short)))
    if len(soon) >= 2 and not short:
        out.append((1, "", "эти диски изнашиваются вместе: менять по одному, не дожидаясь 90% на всех"))
    if soon and not short and (wt := disk_wear.writer_text(writers)):
        out.append((1, "", wt))

    ts = _int(block.get("ts"))
    for m in block.get("missing") or []:
        if not isinstance(m, dict):
            continue
        ser = str(m.get("serial") or "?")
        ago = max(0, ts - _int(m.get("last")))
        when = f"{ago // 3600} ч назад" if ago >= 3600 else f"{max(1, ago // 60)} мин назад"
        title = str(m.get("dev") or "?") if short else _disk_title(m)
        out.append((3, f"missing:{ser}", f"пропал диск {title}, последний раз виден {when}"))
    out.sort(key=lambda p: -p[0])
    return out


# DNS ноды (агент 2.25): через какие резолверы нода ходит и как быстро они отвечают, фоновый
# замер. Повод - сбой рекурсивных DNS Hetzner 08.10.2026: имена резолвились по 5 секунд, k8s не
# мог скачать образы. Медленно - дольше 1,5 с: для программы это уже подвисание, а glibc ждет
# резолвер до 5 секунд. Проблема - когда медленно или совсем не резолвится имя не из кэша (то,
# что почувствуют программы) или когда тормозят все резолверы ноды разом. Один медленный из
# нескольких - видно в карточке, но не алерт: systemd-resolved уйдет на соседний.
_DNS_SLOW_MS = 1500
# Имя не из кэша резолвер ищет у авторитетных серверов домена панели, и путь до них бывает
# длинным: с нод в Hetzner новые имена example.com и example.org (их DNS в России) резолвятся
# 0,5-1,5 с и в обычный день. Поломка - 3 с и больше: при сбое Hetzner было 5 с.
_DNS_MISS_SLOW_MS = 3000
_DNS_FRESH = 5 * 60       # снимок старше - агент его не обновляет, не судим
_DNS_MISS_FRESH = 12 * 60  # резолв не из кэша мерится раз в 5 минут
_DNS_HOLD = 10 * 60       # держится 10 минут - алерт: разовый медленный ответ бывает у любого


def dns_block(rep: dict) -> dict | None:
    """Свежий снимок DNS ноды из отчета или None."""
    d = rep.get("dns")
    if not isinstance(d, dict):
        return None
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ref - float(d.get("ts") or 0) > _DNS_FRESH:
        return None
    return d


def _dns_answer(x: dict) -> str:
    ms = int(x.get("ms") if x.get("ms") is not None else -1)
    if ms >= 0:
        return f"{ms} мс"
    err = str(x.get("err") or "")
    return "нет ответа" if err in ("", "timeout") else err


def dns_slow_servers(d: dict) -> list[dict]:
    """Резолверы, которые отвечают дольше порога или не отвечают."""
    out = []
    for x in d.get("servers") or []:
        if not isinstance(x, dict) or not x.get("addr"):
            continue
        ms = int(x.get("ms") if x.get("ms") is not None else -1)
        if ms < 0 or ms > _DNS_SLOW_MS:
            out.append(x)
    return out


def dns_problem(rep: dict) -> str:
    """Что не так с DNS ноды, словами; пусто - все в порядке или судить не по чему."""
    d = dns_block(rep)
    if d is None:
        return ""
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    servers = [x for x in d.get("servers") or [] if isinstance(x, dict) and x.get("addr")]
    slow = dns_slow_servers(d)
    miss_ts = float(d.get("miss_ts") or 0)
    miss = int(d.get("miss_ms") if d.get("miss_ms") is not None else 0)
    # Быстрый ответ ошибкой (SERVFAIL, REFUSED) на несуществующее имя - не поломка: так отвечает
    # DNS меша 100.100.100.100, и systemd-resolved отдает SERVFAIL, хотя корпоративный резолвер
    # рядом сказал NXDOMAIN, а настоящие имена резолвятся (corp-ai-dev). Поломка -
    # ответа нет совсем или он дольше 3 с.
    miss_err = str(d.get("miss_err") or "")
    miss_bad = miss_ts > 0 and ref - miss_ts <= _DNS_MISS_FRESH and (
        miss > _DNS_MISS_SLOW_MS or (miss < 0 and miss_err in ("", "timeout")))
    all_bad = bool(servers) and len(slow) == len(servers)
    if not miss_bad and not all_bad:
        return ""
    parts = []
    if miss_bad:
        parts.append("имя не из кэша " + (
            f"не резолвится ({_dns_answer({'ms': -1, 'err': d.get('miss_err')})})" if miss < 0
            else f"резолвится {miss} мс"))
    if slow:
        parts.append("резолверы: " + ", ".join(f"{x['addr']} {_dns_answer(x)}" for x in slow))
    return "; ".join(parts)


def dns_suspects(rep: dict) -> set[str]:
    """Резолверы, на которые падает подозрение при поломке DNS ноды: медленные, а если на прямые
    запросы все отвечают быстро (из кэша), а не резолвится имя не из кэша, - все резолверы ноды.
    Так было вечером 08.10.2026 у Hetzner: кэш отвечал мгновенно, новые имена не резолвились у
    четырех нод, а подсказка про сбой у провайдера не сработала ни разу. Свои резолверы ноды
    (127.0.0.1, DNS меша 100.100.100.100) у каждой ноды свои, по ним ноды не сравниваем."""
    d = dns_block(rep) or {}
    slow = {x["addr"] for x in dns_slow_servers(d)}
    every = {x["addr"] for x in d.get("servers") or [] if isinstance(x, dict) and x.get("addr")}
    return {a for a in (slow or every) if not a.startswith("127.") and a != "100.100.100.100"}


def dns_others_hint(sid: int, slow_by: dict[int, set[str]]) -> str:
    """У скольких еще нод сейчас тормозят те же резолверы: тогда это сбой у провайдера."""
    mine = slow_by.get(sid) or set()
    if not mine:
        return ""
    n = sum(1 for other, addrs in slow_by.items() if other != sid and addrs & mine)
    return f". Так же у {n} других нод с теми же резолверами, похоже на сбой у провайдера" if n >= 2 else ""


def dns_dead_resolvers(rep: dict, tracked: dict | None, now: datetime) -> list[tuple[dict, str]]:
    """Резолверы, которые тормозят или молчат дольше _DNS_HOLD подряд: [(замер, с какого
    момента)]. tracked - alert_state dns_res, его ведет цикл алертов."""
    d = dns_block(rep)
    if d is None or not isinstance(tracked, dict):
        return []
    out = []
    for x in dns_slow_servers(d):
        since = tracked.get(x["addr"])
        t0 = _parse_iso(since) if isinstance(since, str) else None
        if t0 is not None and (now - t0).total_seconds() >= _DNS_HOLD:
            out.append((x, since))
    return out


def _dns_res_text(x: dict) -> str:
    ans = _dns_answer(x)
    if ans == "нет ответа":
        return "не отвечает"
    return f"отвечает {ans}" if ans.endswith(" мс") else f"отвечает ошибкой {ans}"


# Сдвиг часов панель меряет по отчету: свое время на приеме минус время ноды на отправке. В
# разницу входит и дорога отчета, а агент шлет каждый отчет новым соединением и перед ним
# резолвит имя панели: когда резолверы Hetzner тормозили (08.10.2026, по 4-6 с), у нод с
# точными часами набегал такой же "сдвиг", и карточка советовала синхронизировать время.
# Дорога только прибавляет, поэтому сдвиг - минимум за 5 минут: хоть один отчет за это время
# доходит быстро. Отчет идет не дольше 15 с (таймаут агента), и два отчета подряд выше минимума
# больше чем на 20 с - это часы перевели назад: окно начинаем заново.
_CLOCK_WIN = 300
_CLOCK_JUMP = 20


def clock_skew_window(prev: dict | None, raw: int, ts: float) -> dict:
    """Окно сдвига часов после нового отчета: {"win": [[время, сдвиг], ...] по возрастанию
    сдвига, первый - минимум за _CLOCK_WIN; "last": сырой сдвиг прошлого отчета}."""
    p = prev if isinstance(prev, dict) else {}
    win = [w for w in p.get("win") or []
           if isinstance(w, list) and len(w) == 2 and ts - w[0] < _CLOCK_WIN]
    last = p.get("last")
    if win and raw - win[0][1] > _CLOCK_JUMP and isinstance(last, int) and last - win[0][1] > _CLOCK_JUMP:
        win = []
    while win and win[-1][1] >= raw:
        win.pop()
    win.append([int(ts), raw])
    return {"win": win, "last": raw}


# Сервер задыхается (агент 2.26). PSI из /proc/pressure - доля времени, когда процессы ждали
# ресурс: some - хоть один, full - все разом (сервер ничего не делал). Берем среднее за минуту,
# сверху выдержка алерта (alert_sustain_seconds): ночной бэкап на пару минут - не повод.
# Пороги - для того, что видно глазами: memory full 10% - уже свопится и тормозит.
_PSI_MEM_FULL = 10.0
_PSI_MEM_SOME = 40.0
_PSI_IO_FULL = 50.0
_PSI_IO_SOME = 80.0
# Зависшие процессы (агент 2.40, dstate.stuck) - как их видит ядро (hung_task): тот же процесс в D
# отчет за отчетом и ни одного переключения контекста дольше двух минут. Само число процессов в D
# не беда: под тяжелым чтением в D каждую секунду другие процессы на миллисекунды, на базе их было
# 2-23 сутками, а не висел ни один (алерт держался три дня). Число остается в карточке справкой и
# у старых агентов тоже не алертит. hung_new - на сколько за 10 минут вырос hung_task_detect_count
# ядра: он растет на каждой проверке, пока задача висит.
_FDS_WARN, _FDS_PROB = 80.0, 90.0


def _stuck_dur(sec: int) -> str:
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{max(sec // 60, 1)} мин"


def dstate_stuck(rep: dict) -> tuple[list[dict], int]:
    """Зависшие процессы (дольше всех первыми) и рост счетчика hung_task ядра за 10 минут."""
    ds = rep.get("dstate") if isinstance(rep.get("dstate"), dict) else {}
    raw = ds.get("stuck")
    stuck = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
    stuck.sort(key=lambda x: -_int(x.get("sec")))
    return stuck, max(_int(ds.get("hung_new")), 0)


def _stuck_text(stuck: list[dict], hung: int) -> str:
    """"завис postgres (PID 4242) в ожидании диска или сети 7 мин, wchan ..." или перечень."""
    def who(x: dict) -> tuple[str, int, str, str]:
        return (str(x.get("comm") or "?"), _int(x.get("pid")), str(x.get("wchan") or ""),
                _stuck_dur(_int(x.get("sec"))))

    if len(stuck) == 1:
        c, pid, w, d = who(stuck[0])
        return f"завис {c} (PID {pid}) в ожидании диска или сети {d}" + (f", wchan {w}" if w else "")
    if stuck:
        n = len(stuck)
        items = [f"{c} (PID {pid}{f', wchan {w}' if w else ''}) {d}" for c, pid, w, d in map(who, stuck[:3])]
        return (f"зависли в ожидании диска или сети {n} {_plural(n, 'процесс', 'процесса', 'процессов')}: "
                + ", ".join(items) + (f" и еще {n - 3}" if n > 3 else ""))
    return f"ядро видит зависшие задачи: hung_task сработал {hung} {_plural(hung, 'раз', 'раза', 'раз')} за 10 мин"


def pressure_hint(rep: dict) -> str:
    """Вторая строка алерта про зависшие: что это значит и куда смотреть."""
    stuck, hung = dstate_stuck(rep)
    if stuck:
        who = "процесс дольше 2 мин в D и ни разу не проснулся" if len(stuck) == 1 else \
            "процессы дольше 2 мин в D и ни разу не проснулись"
        return (f"{who}: так висят на умирающем диске, подвисшем NFS или хранилище. "
                f"На чем висит: sudo cat /proc/{_int(stuck[0].get('pid'))}/stack и dmesg")
    if hung:
        return ("ядро видит задачу в D дольше hung_task_timeout_secs: процесс и стек в dmesg "
                "(blocked for more than)")
    return ""


def pressure_problems(rep: dict) -> list[tuple[int, str, str]]:
    """(уровень, ресурс, текст) по свежему отчету: память и диск по PSI, зависшие процессы,
    файловые дескрипторы. Пусто - все в норме или агент старше 2.26."""
    out: list[tuple[int, str, str]] = []
    psi = rep.get("psi") if isinstance(rep.get("psi"), dict) else {}

    def val(k: str) -> float:
        try:
            return float(psi.get(k) or 0)
        except (TypeError, ValueError):
            return 0.0

    mf, ms = val("mem_full_60"), val("mem_some_60")
    if mf >= _PSI_MEM_FULL:
        out.append((2, "mem", f"не хватает памяти: все процессы разом стоят {mf:.0f}% времени"))
    elif ms >= _PSI_MEM_SOME:
        out.append((1, "mem", f"не хватает памяти: процессы ждут память {ms:.0f}% времени"))
    io_f, io_s = val("io_full_60"), val("io_some_60")
    if io_f >= _PSI_IO_FULL:
        out.append((2, "io", f"диск не успевает: все процессы разом ждут диск {io_f:.0f}% времени"))
    elif io_s >= _PSI_IO_SOME:
        out.append((1, "io", f"диск не успевает: процессы ждут диск {io_s:.0f}% времени"))
    stuck, hung = dstate_stuck(rep)
    if stuck or hung:
        out.append((2, "dstate", _stuck_text(stuck, hung)))
    fd = rep.get("fds") if isinstance(rep.get("fds"), dict) else {}
    used, lim = _int(fd.get("used")), _int(fd.get("max"))
    if lim > 0:
        pct = used / lim * 100
        if pct >= _FDS_WARN:
            out.append((2 if pct >= _FDS_PROB else 1, "fds",
                        f"файловые дескрипторы заняты на {pct:.0f}% ({used} из {lim})"))
    out.sort(key=lambda x: -x[0])
    return out


# Ноды кластера (агент 2.26 отдает условия): kubelet под давлением уже выселяет поды или не
# пускает новые, NotReady - на ноде не работает ничего нового
_KUBE_NODE_COND = {
    "MemoryPressure": "не хватает памяти (MemoryPressure)",
    "DiskPressure": "мало места на диске (DiskPressure)",
    "PIDPressure": "кончаются PID (PIDPressure)",
    "NetworkUnavailable": "сеть не настроена (NetworkUnavailable)",
}


def kube_node_problems(rep: dict) -> list[str]:
    """'нода w1 не готова (NotReady)', 'нода w2: мало места на диске (DiskPressure)'."""
    kube = rep.get("kube") or {}
    if not kube.get("access"):
        return []
    out = []
    for n in kube.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        name = n.get("name") or "?"
        if n.get("ready") is False:
            out.append(f"нода {name} не готова (NotReady)")
        conds = [_KUBE_NODE_COND.get(str(c), str(c)) for c in n.get("pressure") or []]
        if conds:
            out.append(f"нода {name}: " + ", ".join(conds))
    return out


def kube_failed_jobs(rep: dict, now: datetime | None = None, hours: int | None = None) -> list[dict]:
    """Упавшие разовые Job кластера (агент 2.26, за неделю); hours - только свежее."""
    kube = rep.get("kube") or {}
    if not kube.get("access"):
        return []
    out = []
    for j in kube.get("failed_jobs") or []:
        if not isinstance(j, dict):
            continue
        ts = _int(j.get("ts"))
        if hours is not None and now is not None and (not ts or now.timestamp() - ts > hours * 3600):
            continue
        out.append(j)
    return out


def failed_jobs_text(jobs: list[dict], limit: int = 3) -> str:
    items = [f"{j.get('ns', '?')}/{j.get('name', '?')}" + (f" ({j['reason']})" if j.get("reason") else "")
             for j in jobs[:limit]]
    if len(jobs) > limit:
        items.append(f"и еще {len(jobs) - limit}")
    return ", ".join(items)


# Сеть ноды. TCP - агент 2.26, ошибки и дропы интерфейсов агент шлет давно. Все в секунду,
# с той же выдержкой, что у CPU и RAM.
_RETRANS_PCT = 10.0       # % сегментов, ушедших повторно: потери на пути
_RETRANS_MIN_OUT = 100.0  # сегментов/с: на тихой ноде доля прыгает от пары потерь
_LISTEN_OVF_MIN = 1.0     # переполнений очереди accept: сервис не успевает принимать
_CT_DROP_MIN = 1.0        # новых соединений, выброшенных conntrack
_IF_ERRS_MIN = 1.0
# Единицы дропов в секунду бывают и у здоровой ноды (чужие протоколы, LLDP), поэтому порог
# высокий; виртуальные мосты и туннели Docker и Kubernetes не считаем
_IF_DROPS_MIN = 100.0
_IF_VIRTUAL = ("docker", "br-", "cali", "flannel", "cni", "kube-", "vxlan", "tunl", "lxc", "virbr", "weave")
# К кому уходят повторы (агент 2.32 смотрит ss -ti, когда их много). Почти все к одному адресу -
# это канал до него, а не сеть сервера: одна большая выгрузка через плохую сеть давала "18%
# сегментов уходят повторно" при здоровом сервере. Такое не алертим, видно в карточке.
_PEER_DOMINANT = 0.8
_PEER_MIN_TOTAL = 30  # повторов за просмотр: по меньшему числу о получателях не судим


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def retrans_peers(tcp: dict) -> tuple[list[dict], float]:
    """Получатели повторов TCP из отчета (агент 2.32) и доля главного из них. Пусто - агент
    старый или повторов слишком мало, чтобы судить."""
    peers = [p for p in (tcp.get("peers") or []) if isinstance(p, dict) and p.get("ip")]
    total = _num(tcp.get("peers_total"))
    if not peers or total < _PEER_MIN_TOTAL:
        return [], 0.0
    return peers, min(_num(peers[0].get("retrans")) / total, 1.0)


def peer_label(p: dict) -> str:
    """'198.51.100.7 (caddy:443)': адрес, процесс и локальный порт, если агент их узнал."""
    what = str(p.get("proc") or "")
    if p.get("port"):
        what = f"{what}:{p['port']}" if what else f"порт {p['port']}"
    return f"{p.get('ip')} ({what})" if what else str(p.get("ip"))


def net_problems(rep: dict) -> list[tuple[int, str, str]]:
    """(уровень, что, текст) по свежему отчету: потери TCP, переполнения очереди accept,
    дропы conntrack, ошибки и дропы интерфейсов."""
    out: list[tuple[int, str, str]] = []
    tcp = rep.get("tcp") if isinstance(rep.get("tcp"), dict) else {}
    ct = _num(tcp.get("ct_drop"))
    if ct >= _CT_DROP_MIN:
        out.append((2, "conntrack", f"conntrack выбрасывает новые соединения: {ct:.0f} в секунду"))
    sent, again = _num(tcp.get("out")), _num(tcp.get("retrans"))
    # Повторы SYN - подключения, на которые никто не ответил, а не потери на пути: торрент-клиент
    # за VPN на ноде-входе давал "28% сегментов уходят повторно" при чистой сети (агент 2.30+)
    syn = min(_num(tcp.get("syn_retrans")), again)
    sent, again = max(sent - syn, 0.0), again - syn
    if sent >= _RETRANS_MIN_OUT and again / sent * 100 >= _RETRANS_PCT:
        peers, top = retrans_peers(tcp)
        if not (peers and top >= _PEER_DOMINANT):
            txt = f"TCP теряет пакеты: {again / sent * 100:.0f}% уходит повторно"
            if peers:
                txt += ", больше всего к " + ", ".join(peer_label(p) for p in peers[:2])
            out.append((1, "retrans", txt))
    ovf = _num(tcp.get("listen_ovf"))
    if ovf >= _LISTEN_OVF_MIN:
        n = round(ovf)
        out.append((1, "listen", f"сервис не успевает принимать соединения: очередь accept "
                                 f"переполняется {n} {_plural(n, 'раз', 'раза', 'раз')} в секунду"))
    for i in rep.get("net_ifaces") or []:
        if not isinstance(i, dict):
            continue
        name = str(i.get("if") or "?")
        errs, drops = _num(i.get("errs")), _num(i.get("drops"))
        if errs >= _IF_ERRS_MIN:
            n = round(errs)
            out.append((1, "iface", f"{name}: {n} {_plural(n, 'ошибка', 'ошибки', 'ошибок')} в секунду"))
        if drops >= _IF_DROPS_MIN and not name.startswith(_IF_VIRTUAL):
            n = round(drops)
            out.append((1, "iface", f"{name}: {n} {_plural(n, 'дроп', 'дропа', 'дропов')} в секунду"))
    out.sort(key=lambda x: -x[0])
    return out


def net_hint(rep: dict, nets: list[tuple[int, str, str]]) -> str:
    """Вторая строка алерта: что это значит и куда смотреть - по главной причине."""
    if not nets:
        return ""
    _lvl, kind, text = nets[0]
    if kind == "retrans":
        if retrans_peers(rep.get("tcp") if isinstance(rep.get("tcp"), dict) else {})[0]:
            return "потери у разных получателей сразу: похоже на канал или сетевую карту сервера, стоит спросить хостера"
        return "соединения тормозят; к кому уходят повторы: ss -ti | grep retrans"
    if kind == "conntrack":
        return "новые подключения к серверу теряются: таблица conntrack забита"
    if kind == "listen":
        return "клиенты ловят таймауты подключения: сервис перегружен или у него мал backlog"
    if "ошиб" in text:
        return "битые пакеты на карте: кабель, порт или драйвер, это к хостеру"
    return "карта не успевает принимать пакеты: мало буферов (ethtool -G) или CPU на прерывания"


# Диск тормозит: задержка ввода-вывода (await) устойчиво высокая. За неделю истории парка
# (13.10.2026) задержка выше 100 мс не держалась нигде даже 5 минут подряд, выше 50 мс - только на
# дисках серверов бэкапов во время бэкапа: порог 100 мс с выдержкой алерта шума не дает.
_DISK_SLOW_MS = 100
_DISK_SLOW_CRIT_MS = 500
_DISK_GLITCH_MS = 60000  # счетчик сбросился или перевалил: 717 секунд на NVMe - не задержка
_DISK_SKIP = ("loop", "ram", "sr", "zram", "fd")


def disk_slow(rep: dict) -> tuple[int, str]:
    """(уровень, текст) по самому медленному диску сейчас; выдержку добавляют вызывающие"""
    worst = None
    for d in (rep or {}).get("disk_devs") or []:
        if not isinstance(d, dict):
            continue
        dev = str(d.get("dev") or "")
        if not dev or dev.startswith(_DISK_SKIP):
            continue
        try:
            aw, util = float(d.get("await") or 0), float(d.get("util") or 0)
        except (TypeError, ValueError):
            continue
        if aw >= _DISK_GLITCH_MS:
            continue
        if worst is None or aw > worst[0]:
            worst = (aw, dev, util)
    if worst is None or worst[0] < _DISK_SLOW_MS:
        return 0, ""
    aw, dev, util = worst
    return (2 if aw >= _DISK_SLOW_CRIT_MS else 1,
            f"диск {dev} тормозит: задержка {round(aw)} мс, загружен на {round(util)}%")


def _held_long(s: Server, state: dict, key: str, now: datetime) -> bool:
    """Держится ли "<вид>_since" дольше выдержки алерта: главная показывает то же, что алертит."""
    since = _parse_iso(state.get(f"{key}_since") or "")
    ss = getattr(s, "alert_sustain_seconds", None)
    hold = _SUSTAIN_DEFAULT if ss is None else max(int(ss), 0)
    return since is not None and (now - since).total_seconds() >= hold


def _routine_tail(state: dict, key: str, now: datetime) -> str:
    """Хвост к беде, которую цикл алертов узнал как распорядок (alert_state "<вид>_quiet")"""
    try:
        q = float(state.get(f"{key}_quiet") or 0)
    except (TypeError, ValueError):
        return ""
    usual = str(state.get(f"{key}_usual") or "")
    return f" (так каждый день в {usual}, алерт не шлем)" if q > now.timestamp() and usual else ""


_IO_BUSY = 20  # диск занят меньше - не он причина, называть его нечестно


def io_cause(rep: dict) -> str:
    """Хвост к давлению на диск: какой диск занят и с какой задержкой. Процессы ждут, а локальные
    диски свободны (ждут сетевую файловую систему или хранилище гипервизора) - так и сказать: строка
    "sda занят на 0%" показывала не туда"""
    devs = [d for d in rep.get("disk_devs") or [] if isinstance(d, dict) and d.get("util") is not None]
    if not devs:
        return ""
    d = max(devs, key=lambda x: float(x.get("util") or 0))
    aw = float(d.get("await") or 0)
    if float(d["util"]) < _IO_BUSY:
        return f" - при этом локальные диски почти свободны (самый занятый {d.get('dev') or '?'} на {float(d['util']):.0f}%)"
    return f" - {d.get('dev') or '?'} занят на {float(d['util']):.0f}%" + (f", задержка {aw:.0f} мс" if aw else "")


def server_problems(s: Server, now: datetime, pod_names: dict[str, str] | None = None,
                    web_err: tuple[float, float, int, dict, float] | None = None) -> list[dict]:
    """Что сломано на сервере коротко, для сводки "Что сломано" на главной и значков в списке
    серверов: все, о чем шлют алерты, кроме того, что главная считает сама (CPU, RAM, диск,
    температуры, conntrack, Docker, поды, бэкапы клиентов и репозитории). Уровни те же, что у
    алертов: 2-3 - проблема, 1 - предупреждение. Выдержки алерта здесь нет там, где она гасит
    только повторы сообщений: главная показывает, что сейчас. Прогноз виден с того же срока, что
    и алерт (_FORECAST_WARN_H, пять суток). У молчащей ноды пусто, про нее говорит "оффлайн".

    section и srv - куда вести, если не в карточку сервера: "kuber" (sec - вкладка кластера),
    "services", "backups" (srv - окно бэкап-сервера, иначе клиента). web_err - окно ошибок 5xx
    этой ноды (web_error_window); без него про 5xx молчим."""
    if not seen_online(s, now):
        return []
    rep = s.last_report or {}
    out: list[dict] = []
    state = s.alert_state or {}

    def add(kind: str, level: int, text: str, sec: str, mute: str, since: str | None = None,
            **route) -> None:
        # since - с какого момента это видно (для "Что сломано": "2 дня"); по умолчанию -
        # когда панель впервые увидела проблему этого вида (alert_state "<вид>_from")
        out.append({"kind": kind, "level": level, "text": text, "sec": sec, "mute": mute,
                    "since": since or state.get(f"{kind}_from"), **route})

    hb = disk_health_block(rep)
    if hb is not None:
        for lvl, _key, txt in disk_health_problems(hb, short=True, wear_hist=s.disk_wear):
            if txt:
                add("disk_health", lvl, txt, "diskhealth", "disk_health")
        n = _int((hb.get("io") or {}).get("count"))
        if n >= _DISK_IO_MIN:
            add("disk_health", 2, f"ошибки ввода-вывода: {n} за 10 минут", "diskhealth", "disk_health")
    ub = units_block(rep)
    if ub is not None:
        ref = float(rep.get("clock_unix") or 0) or now.timestamp()
        muted = units_muted(s, now)
        for u in ub.get("units") or []:
            if not isinstance(u, dict) or not u.get("unit") or u["unit"] in muted or benign_unit(str(u["unit"])):
                continue
            if ref - float(u.get("since") or 0) < _UNIT_MIN_AGE:
                continue
            u_since = datetime.fromtimestamp(float(u["since"]), timezone.utc).isoformat() if u.get("since") else None
            add("units", unit_level(u), f"{u['unit']}: {unit_why(u)}", "units", f"unit:{u['unit']}", u_since)
    # Юниты в цикле перезапусков: приросты счетчика ведет цикл алертов (alert_state unit_loops)
    rs = units_restarting(rep)
    loops = state.get("unit_loops") if isinstance(state.get("unit_loops"), dict) else {}
    for u in rs or []:
        name = str(u["unit"])
        hits = unit_loop_hits(loops.get(name), now)
        if len(hits) < _UNIT_LOOP_MIN or name in units_muted(s, now):
            continue
        count, why = unit_loop_text(u, hits)
        add("unit_loop", 2, f"{name} перезапускается в цикле: {count} за {_UNIT_LOOP_WINDOW // 60} минут{why}",
            "unitloop", f"unit:{name}", str(hits[0][0]), unit=name,
            log=[str(x)[:200] for x in (u.get("log") or [])[-5:]])
    for g in spin_groups(rep):
        # крутится почти всю жизнь (так он и попал в группу), поэтому начало - возраст процесса
        age = int(g.get("spin_age") or 0)
        add("cpu_spin", 1, spin_text([g], pod_names, with_age=False), "cpueat", "cpu_spin",
            (now - timedelta(seconds=age)).isoformat() if age > 0 else None)
    for i in dfc.fresh_items(s.disk_forecast, now) or []:
        eta = float(i["eta_h"])
        if eta > _FORECAST_WARN_H:
            continue
        what = (f"inode на {i['mount']} кончатся" if i.get("kind") == "inode"
                else f"{i['mount']} заполнится")
        add("disk_forecast", 2 if eta <= _FORECAST_CRIT_H else 1,
            f"{what} {dfc.eta_text(eta)}", "diskfill", "disk_forecast")
    ino = [d for d in rep.get("disks") or [] if isinstance(d, dict) and d.get("inodes")]
    if ino:
        wi = max(ino, key=lambda d: (d.get("inodes_used") or 0) / d["inodes"])
        ipct = round((wi.get("inodes_used") or 0) / wi["inodes"] * 100)
        crit, prob, warn = s.disk_crit_percent, s.disk_alert_percent, s.disk_warn_percent
        lvl = 3 if crit and ipct >= crit else 2 if prob and ipct >= prob else 1 if warn and ipct >= warn else 0
        if lvl:
            add("inode", lvl, f"inode {wi.get('mount') or '?'} {ipct}%", "diskfill", f"inode@{lvl}" if lvl < 3 else "inode")
    # Часы: пороги алерта (5 с, 30 с, 5 минут) без его выдержки. Сдвиг уже очищен от дороги
    # отчета (clock_skew_window), а сами часы за минуту не уходят и не возвращаются.
    skew = rep.get("clock_skew_sec")
    if isinstance(skew, (int, float)):
        a = abs(int(skew))
        lvl = 3 if a >= 300 else 2 if a >= 30 else 1 if a >= 5 else 0
        if lvl:
            disp = f"{a} с" if a < 120 else f"{a // 60} мин"
            add("clock", lvl, f"часы разошлись с панелью на {disp}", "clock",
                "clock" if lvl >= 3 else f"clock@{lvl}", state.get("clock_since"))
    # DNS: поломка видна сразу предупреждением, проблемой - после выдержки алерта (начало ведет
    # цикл алертов, dns_since). Резолвер, который лежит дольше выдержки, пока соседние отвечают, -
    # предупреждение: имена резолвятся, но запасного нет.
    why = dns_problem(rep)
    if why:
        d_since = _parse_iso(state.get("dns_since") or "")
        held = d_since is not None and (now - d_since).total_seconds() >= _DNS_HOLD
        add("dns", 2 if held else 1, f"DNS: {why}", "", "dns" if held else "dns@1", state.get("dns_since"))
    else:
        dead = dns_dead_resolvers(rep, state.get("dns_res"), now)
        if len(dead) == 1:
            add("dns", 1, f"DNS: резолвер {dead[0][0]['addr']} {_dns_res_text(dead[0][0])}", "", "dns@1",
                dead[0][1], sub="resolver")
        elif dead:
            add("dns", 1, "DNS: резолверы " + ", ".join(f"{x['addr']} {_dns_answer(x)}" for x, _ in dead),
                "", "dns@1", min(since for _, since in dead), sub="resolver")
    # Коннекты СУБД: порог алерта без его выдержки, как CPU и RAM на главной
    conn_thr = getattr(s, "db_conn_alert_percent", 0) or 0
    worst = db_conn_worst(rep) if conn_thr else None
    if worst is not None and worst[0] >= conn_thr:
        pct, db = worst
        add("db_conn", 2, f"{db.get('container') or db.get('engine') or 'СУБД'}: занято "
            f"{db.get('conn_used') or 0} из {db.get('conn_max') or 0} подключений ({round(pct)}%)",
            "db", "db_conn", state.get("db_conn_since"), section="services")
    # Ошибки 5xx: окно алерта (15 минут), без часа после него, который держит алерт одной историей
    w5 = web_5xx_now(s, web_err, now)
    if w5 is not None and w5[0] and w5[2]:
        # почему - по последней минуте, если helper 0.26 ее назвал
        why = web_upstream.text(web_upstream.block_acc(
            rep.get("extras"), web_muted_keys(s, now), lambda x: web_label_key(web_log_label(x))), counts=False)
        add("web_5xx", 2, f"ошибки 5xx: {w5[1] / w5[2] * 100:.1f}% запросов за 15 минут"
            + (f" - {why}" if why else ""), "web", "web_5xx")
    # Очереди выше своего порога (RabbitMQ и Redis), как у алерта queue; заглушенные не в счет
    hot = []
    qmuted = queue_watch.muted_keys(s, now)
    for b in queue_watch.brokers(rep):
        for q in b["queues"]:
            if not isinstance(q, dict):
                continue
            key = queue_key(b["source"], q)
            thr = queue_threshold(s, key)
            if thr > 0 and queue_depth(q) >= thr and key not in qmuted:
                hot.append(f"{q.get('name') or '?'} ({queue_depth(q)})")
    if hot:
        add("queue", 2, "переполнены очереди: " + ", ".join(hot[:3])
            + (f" и еще {len(hot) - 3}" if len(hot) > 3 else ""), "queues", "queue", section="services")
    # Очередь растет и не разбирается (ход - в alert_state qwatch, ведет цикл алертов)
    for x in queue_watch.problems(s, now):
        add("queue_stuck", x["level"], x["text"], "queues", queue_watch.MUTE_PREFIX + x["key"], x["since"],
            section="services")
    # Всплеск упавших задач (helper queues-setup 0.4): за час в разы больше обычного
    for x in queue_watch.failed_problems(s, now):
        add("queue_failed", x["level"], x["text"], "queues", queue_watch.FAIL_PREFIX + x["key"], section="services")
    # Воркеры под supervisor и pm2 (helper workers-setup): не запускается, падает и поднимается
    for x in workers_mod.problems(s, now):
        add("workers", x["level"], x["text"], "workers", workers_mod.MUTE_PREFIX + x["key"], x["since"],
            section="services")
    # Кроны (helper cron-setup): копии наслаиваются или одна висит в разы дольше обычного
    for x in cron_watch.problems(s, now):
        add("cron_stuck", x["level"], x["text"], "cron", cron_watch.MUTE_PREFIX + x["key"], x["since"],
            section="services")
    # Всплеск ошибок в логах контейнеров и подов (helper logerr-setup): за час в разы больше обычного
    for x in log_errors.problems(s, now):
        add("log_errors", x["level"], x["text"], "logs", log_errors.MUTE_PREFIX + x["key"], section="services")
    # PHP-FPM (helper fpm-setup): пул подолгу упирается в pm.max_children или рвет запросы по таймауту
    for x in fpm_pools.problems(s, now):
        add("fpm", x["level"], x["text"], "fpm", fpm_pools.MUTE_PREFIX + x["key"], section="services")
    # Сроки кластера: ближайший в пределах дальнего порога. За последний порог и истекший - проблема
    wd = kube_warn_days(s) if getattr(s, "kube_expiry_warn_days", None) else []
    soon = kube_expiry_soon(rep, wd, now) if wd else None
    if soon is not None:
        c = kube_expiry_ctx(*soon)
        add("kube_expiry", 3 if soon[1] < 0 else 2 if soon[1] <= wd[-1] else 1,
            f"{c['what']} {c['where']}: {c['value']}{c['more']}", "expiry", "kube_expiry", section="kuber")
    # Flux: после выдержки алерта (<вид>_from) - пока идет выкат, зависимые сборки минутами
    # отвечают DependencyNotReady, и строка мигала бы на каждый деплой
    fx = flux_state(rep) if rep.get("flux") is not None else None
    if fx is not None and state.get(f"{fx[0]}_from"):
        kind, c = fx
        what = "доставка встала" if kind == "flux_down" else "недоступен репозиторий чартов"
        add(kind, 2 if kind == "flux_down" else 1, f"Flux: {what}, {c['what']} {c['where']} - {c['reason']}{c['more']}",
            "flux", kind, state.get(f"{kind}_since"), section="kuber")
    # Дампы СУБД: сломанный - после выдержки алерта (на стыке прогонов бывает тик без свежего
    # дампа), пропущенный из-за места - сразу
    broken, _why, skipped, free = dump_issues(rep, now)
    if broken and state.get("backup_dump_from"):
        add("backup_dump", 2, "дамп СУБД не обновляется: " + ", ".join(broken), "coverage", "backup_dump",
            state.get("backup_dump_since"), section="backups", srv=False)
    if skipped:
        add("backup_dump_space", 2, f"дамп пропущен, мало места (свободно {free}%): " + ", ".join(skipped),
            "coverage", "backup_dump_space", section="backups", srv=False)
    if (rep.get("kube") or {}).get("access"):
        cron = _cron_dump_problems(rep, now)
        if cron:
            add("backup_cron", 2, "дамп-CronJob не отрабатывает: " + "; ".join(cron[:3])
                + (f" и еще {len(cron) - 3}" if len(cron) > 3 else ""), "coverage", "backup_cron",
                section="backups", srv=False)
        kc = sorted(f"{n} ({why})" for n, why in cron_job_issues(rep, now, dumps=False).items())
        if kc:
            add("kube_cron", 1, ("CronJob не отрабатывает: " if len(kc) == 1 else "CronJob не отрабатывают: ")
                + "; ".join(kc[:3]) + (f" и еще {len(kc) - 3}" if len(kc) > 3 else ""), "cron", "kube_cron",
                section="kuber")
        kn = kube_node_problems(rep)
        if kn and _held_long(s, state, "kube_node", now):
            add("kube_node", 2, "; ".join(kn[:3]) + (f" и еще {len(kn) - 3}" if len(kn) > 3 else ""),
                "nodes", "kube_node", state.get("kube_node_since"), section="kuber")
        fj = kube_failed_jobs(rep, now, 24)
        if fj:
            add("kube_job", 1, ("упал Job: " if len(fj) == 1 else "упали Job: ") + failed_jobs_text(fj),
                "cron", "kube_job", section="kuber")
    # Бэкап-сервер: не прошла проверка целостности репозитория
    bsrv = rep.get("backup_server") or {}
    if bsrv.get("present"):
        bad = failed_checks(bsrv, bsrv_extra(rep), set(getattr(s, "backup_repo_mutes", None) or []), now)
        if bad:
            add("backup_check", 2, "не прошла проверка целостности: " + ", ".join(bad[:3])
                + (f" и еще {len(bad) - 3}" if len(bad) > 3 else ""), _BSRV_SEC["backup_check"], "backup_check",
                section="backups", srv=True)
    # Сервер задыхается: как и у алерта, только то, что держится дольше выдержки
    pr = pressure_problems(rep)
    if pr and _held_long(s, state, "pressure", now):
        add("pressure", pr[0][0], "; ".join(t for _l, _r, t in pr[:3]) + _routine_tail(state, "pressure", now),
            "pressure", "pressure", state.get("pressure_since"))
    nets = net_problems(rep)
    if nets and _held_long(s, state, "net_health", now):
        add("net_health", nets[0][0], "; ".join(t for _l, _r, t in nets[:3]),
            "neterr" if nets[0][1] == "iface" else "nethealth", "net_health", state.get("net_health_since"))
    lk = netlinks.problems(getattr(s, "links_state", None))
    if lk and _held_long(s, state, "link", now):
        add("link", lk[0][0], "; ".join(t for _l, _i, t in lk[:3]), "links", "link", state.get("link_since"))
    tl, tt = traffic_mod.problem(getattr(s, "traffic_sum", None), now)
    if tl and _held_long(s, state, "traffic", now):
        add("traffic", tl, tt, "traffic", "traffic", state.get("traffic_since"))
    # Проверка восстановления бэкапа (helper backup-setup 0.34): ведет в "Бэкапы" клиента
    bv = backup_verify.problems(rep, now)
    if bv:
        add("backup_restore", bv[0][0], "; ".join(t for _l, t in bv[:2]), "restore", "backup_restore",
            section="backups", srv=False)
    # Пропал порт, который слушался неделями
    for x in listen_watch.problems(s, now):
        add("listen_gone", 1, x["text"], "exposure", listen_watch.MUTE_PREFIX + x["port"])
    # Диск тормозит: как и у алерта, только то, что держится дольше выдержки
    dl, dtext = disk_slow(rep)
    if dl and _held_long(s, state, "disk_slow", now):
        add("disk_slow", dl, dtext + _routine_tail(state, "disk_slow", now), "disklat", "disk_slow",
            state.get("disk_slow_since"))
    # GPU (helper gpu-setup): драйвер, пропавшая карта, Xid и сбойная память - сразу, перегрев -
    # как у алерта, когда держится дольше выдержки
    for lv, txt, _k, g_since in gpu_mod.problems(rep, now):
        add("gpu", lv, txt, "gpu", "gpu", g_since)
    hl, htext = gpu_mod.heat(rep, now)
    if hl and _held_long(s, state, "gpu_heat", now):
        add("gpu_heat", hl, htext, "gpu", "gpu_heat", state.get("gpu_heat_since"))
    # LLM-сервисы (агент 2.36): ведут в "Сервисы" ноды; как и алерт - то, что держится дольше выдержки
    if gpu_mod.llm_list(rep) and _held_long(s, state, "llm", now):
        for lv, txt, _k in gpu_mod.llm_problems(rep, state.get("llm_base")):
            add("llm", lv, txt, "llm", "llm", state.get("llm_since"), section="services")
    # Сертификат nginx истекает: ведет в "Сервисы" ноды
    for lv, txt, _key in web_certs.problems(rep, now):
        add("web_cert", lv, txt, "certs", "web_cert", section="services")
    # Конфиг веб-сервера не проходит проверку: ведет в "Сервисы" ноды
    for lv, txt, _key in web_config.problems(rep, now):
        add("web_config", lv, txt, "web", "web_config", section="services")
    # Здоровье баз: ведет в "Сервисы" ноды, где инвентарь СУБД
    for lv, txt, _key in db_health_problems(s, rep, now):
        add("db_health", lv, txt, "db", "db_health", section="services")
    # SSH: root по паролю на открытом порту, перебор паролей без fail2ban
    for lv, txt in ssh_audit.problems(s, now):
        add("ssh_risk", lv, txt, "ssh", "ssh_risk")
    # Второй root, пустой пароль (helper changes-setup): ведет в "Доступы" карточки
    for lv, txt in access_audit.problems(s):
        add("access_risk", lv, txt, "access", "access_risk")
    # Утечка памяти: контейнер ведет в "Докер", под - в "Кубер", процесс - в "Память" карточки
    for x in mem_trend.problems(s, now):
        if not x["level"]:
            continue  # растет, но памяти сервера хватит надолго: видно в карточке, не беда
        mute = mem_trend.MUTE_PREFIX + x["key"]
        if x["kind"] == "docker":
            add("mem_leak", x["level"], x["text"], "containers", mute, section="docker")
        elif x["kind"] == "pod":
            add("mem_leak", x["level"], x["text"], "pods", mute, section="kuber")
        else:
            add("mem_leak", x["level"], x["text"], "memleak", mute)
    # Часто перезапускается (медленный цикл): контейнер ведет в "Докер", под - в "Кубер"
    for x in srv_restarts.problems(s, now):
        if x["kind"] == "docker":
            add("docker_restarts", 1, x["text"], "containers", srv_restarts.MUTE_PREFIX + x["key"], section="docker")
        else:
            add("kube_restarts", 1, x["text"], "pods", srv_restarts.MUTE_PREFIX + x["key"], section="kuber")
    # Изменился доступ: держится сутки, пока не отметили "это мы"
    acc = srv_changes.unacked_alerts(s, now)
    if acc:
        add("access_change", 2, "изменился доступ: " + srv_changes.events_text(acc, 3), "changes", "access_change",
            acc[-1].get("ts"))
    # Что открыто в интернет: внешняя проверка портов (open_ports, раз в 6 часов)
    ex = open_ports.exposed(s, now)
    if ex:
        add("exposed", open_ports.exposed_level(ex), open_ports.exposure_text(ex, 3), "exposure", "exposed")
    # новый порт в интернет - красное: открытый только белому списку с адресом проверки сюда уже
    # не попадает (open_ports.only_allowlist), остальное открыто всем
    fresh = open_ports.new_ports(s, now)
    if fresh:
        add("port_new", 2, "в интернет открылся новый порт: " + open_ports.new_text(fresh), "exposure", "port_new",
            open_ports.new_since(s))
    out.sort(key=lambda p: -p["level"])
    return out


def disk_health_text(probs: list[tuple[int, str, str]], limit: int = 5) -> str:
    """Первая беда - в строке алерта, остальные - строками ниже: "RAID md42, md43 (raid1):
    работает 1 из 2 дисков\n↳ диск nvme1n1 (...) не определяется"."""
    lines = [txt for _lvl, _key, txt in probs if txt]
    if len(lines) > limit:
        lines = lines[:limit] + [f"и еще {len(lines) - limit}"]
    return "\n↳ ".join(lines)


# Упавшие юниты systemd (helper units-setup, раз в минуту). Старше 10 минут - helper
# встал, судить не по чему.
_UNITS_MAX_AGE = 10 * 60
# Юнит, упавший только что, могут как раз чинить руками: ждем пару минут
_UNIT_MIN_AGE = 120
_UNIT_MUTE_PREFIX = "unit:"
# Почему упал: Result из systemd словами
_UNIT_RESULT = {
    "timeout": "не уложился во время", "core-dump": "упал с дампом памяти",
    "start-limit-hit": "слишком часто перезапускался", "resources": "не хватило ресурсов",
    "oom-kill": "убит из-за нехватки памяти", "watchdog": "перестал отвечать watchdog",
    "exit-code": "", "signal": "", "protocol": "нарушил протокол запуска",
}
_SIGNALS = {6: "ABRT", 9: "KILL", 11: "SEGV", 15: "TERM"}

# Свои helper'ы панели: снимки для отчета по таймеру и обработчики кнопок (*-req). Упавший
# helper - ошибка Kervax, а не сервера: в "Что сломано" он виден, алерта нет. changes-setup 0.5,
# который не укладывался во время на нодах с большим журналом, прислал алерт с каждой такой
# ноды, и каждые 10 минут снова. kervax-agent и kervax-dumps (дампы баз для бэкапа) не тут.
_OWN_HELPER_UNIT = re.compile(
    r"kervax-(changes|cron|db-stats|disk-health|disk-usage|fpm|gpu|kube-expiry|kube-flux|logerr|queues|sockfix|units|web-rate|web-sites|workers"
    r"|[a-z]+-req)\.service")

# Задание по таймеру, которое падает на каждом запуске, между падениями несколько минут
# работает и в failed не числится: уходили "упавших юнитов больше нет" и через пару минут
# снова "упал". Юнит, про который уже сказали, считается упавшим еще 20 минут после того,
# как пропал из упавших.
_UNIT_RECOVER_HOLD = 1200


def own_helper_unit(name: str) -> bool:
    return bool(_OWN_HELPER_UNIT.fullmatch(name))


# Служебные таймеры дистрибутива, упавший прогон которых ни на что не влияет и никто на него не
# реагирует: метаданные прошивок fwupd (на виртуалке и за закрытым CDN падает всегда), новости для
# motd, кэш ESM. fwupd-refresh приходил алертом "упал юнит" с ноды, откуда не открывается
# cdn.fwupd.org. Не алертят и в "Что сломано" не попадают; в разделе юнитов видны как есть.
_BENIGN_UNITS = frozenset({
    "fwupd-refresh.service", "motd-news.service", "apt-news.service", "esm-cache.service",
    "update-notifier-download.service", "update-notifier-motd.service",
})


def benign_unit(name: str) -> bool:
    return name in _BENIGN_UNITS


# Юниты бэкапа: про упавший прогон уже говорят алерты бэкапа ("последний бэкап завершился с
# ошибкой", свои бэкапы ноды), второй алерт по тому же прогону не нужен. Основной сервис - тот
# же список, по которому его ищет агент.
_BACKUP_SERVICES = ("systemd-rest.service", "restic-backup.service", "restic.service")


def backup_units(s: Server, rep: dict) -> set[str]:
    out = set(_BACKUP_SERVICES) if (rep.get("backup") or {}).get("configured") else set()
    for j in custom_backups.jobs_of(s):
        u = str(j.get("unit") or "")
        if u:
            out.add(u)
    return out


# Медленные счетчики за сутки: новый объект в той же беде в течение часа после сообщения -
# та же история, отдельного сообщения нет (см. цикл алертов)
_SIG_STORY = frozenset({"docker_restarts", "kube_restarts", "db_health"})
_SIG_STORY_S = 3600

# Распорядок: давление, тормозящий диск или CPU каждый день в одно и то же время (плановый бэкап,
# проверка репозитория) - не новость. На сервере бэкапов "диск не успевает" приходил каждый день в
# 13:13 и отбивался к 15:00. Эпизод, который начался в то же время (плюс-минус _ROUTINE_SLACK) в
# каждый из _ROUTINE_DAYS прошлых дней и каждый раз прошел сам не дольше чем за _ROUTINE_MAX, не
# алертит, и отбоя у него нет. Держится на _ROUTINE_OVER дольше обычного конца - алерт с пометкой.
# История эпизодов - alert_state "<вид>_hist" ([начало, конец] в unix, за _ROUTINE_KEEP)
_ROUTINE_KINDS = frozenset({"pressure", "disk_slow", "cpu"})
_ROUTINE_DAYS = 2
_ROUTINE_SLACK = 45 * 60
_ROUTINE_MAX = 6 * 3600
_ROUTINE_OVER = 3600
_ROUTINE_KEEP = 8 * 86400


def _hist_pairs(hist: object) -> list[tuple[float, float | None]]:
    out = []
    for x in hist if isinstance(hist, list) else []:
        try:
            out.append((float(x[0]), None if x[1] is None else float(x[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def routine_start(hist: object, now_ts: float) -> list:
    """История с новым эпизодом, без старше _ROUTINE_KEEP"""
    keep = [[a, b] for a, b in _hist_pairs(hist) if a > now_ts - _ROUTINE_KEEP]
    return (keep + [[now_ts, None]])[-40:]


def routine_end(hist: object, now_ts: float) -> list:
    """История, где у последнего эпизода есть конец"""
    h = [[a, b] for a, b in _hist_pairs(hist)]
    if h and h[-1][1] is None:
        h[-1][1] = now_ts
    return h


def routine_of(hist: object, now_ts: float) -> tuple[float, float] | None:
    """Обычные начало и конец такого эпизода сегодня (unix), если он был в это же время в каждый из
    _ROUTINE_DAYS прошлых дней и проходил сам; иначе None"""
    pairs = _hist_pairs(hist)
    spans = []
    for d in range(1, _ROUTINE_DAYS + 1):
        t = now_ts - d * 86400
        hit = [(a, b) for a, b in pairs if b is not None and abs(a - t) <= _ROUTINE_SLACK and 0 < b - a <= _ROUTINE_MAX]
        if not hit:
            return None
        a, b = min(hit, key=lambda x: abs(x[0] - t))
        spans.append((a + d * 86400, b + d * 86400))
    return min(a for a, _ in spans), max(b for _, b in spans)


def _sig_all_quiet(s: Server, key: str, sig: set[str], now: datetime) -> bool:
    """Все, о чем сказал алерт, теперь заглушено человеком или не алертит по замыслу (свой
    helper): уровень упал не потому, что стихло, и отбой слать нельзя."""
    if key == "units":
        muted = units_muted(s, now)
        return all(own_helper_unit(u) or benign_unit(u) or u in muted for u in sig)
    if key in ("docker_restarts", "kube_restarts"):
        muted = srv_restarts.muted_keys(s)
        return all(podnames.muted(muted, k) for k in sig)
    if key == "queue_stuck":
        muted = set(s.alert_mutes or [])
        return all(queue_watch.MUTE_PREFIX + k in muted for k in sig)
    if key == "queue_failed":
        return all(k in queue_watch.failed_muted(s, now) for k in sig)
    if key == "workers":
        return all(k in workers_mod.muted_keys(s, now) for k in sig)
    if key == "cron_stuck":
        return all(k in cron_watch.muted_keys(s, now) for k in sig)
    if key == "log_errors":
        return all(k in log_errors.muted_keys(s, now) for k in sig)
    if key == "fpm":
        return all(k in fpm_pools.muted_keys(s, now) for k in sig)
    if key == "mem_leak":
        # растет как росло, но памяти теперь хватает (или правило 1.4.278: раньше алертил любой
        # рост): "память больше не растет" было бы неправдой
        muted = {m[len(mem_trend.MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(mem_trend.MUTE_PREFIX)}
        growing = {x["key"] for x in mem_trend.problems(s, now)}
        return all(k in growing or podnames.muted(muted, k) for k in sig)
    return False


def units_hold(s: Server, bad: list[dict], now: datetime, muted: set[str]) -> dict[str, list]:
    """Юниты для алерта с выдержкой отбоя: {юнит: [с какого момента его нет среди упавших
    или None, уровень]}. Пропавший держится, пока не прошла выдержка; держим только те, про
    которые алерт уже ушел (units_sig). Заглушенный уходит сразу."""
    st = s.alert_state or {}
    prev = st.get("units_hold") if isinstance(st.get("units_hold"), dict) else {}
    out: dict[str, list] = {u["unit"]: [None, unit_level(u)] for u in bad}
    for name in sorted(set(st.get("units_sig") or []) - out.keys()):
        if name in muted or own_helper_unit(name) or benign_unit(name):
            continue
        p = prev.get(name)
        p = p if isinstance(p, list) and len(p) == 2 else [None, 1]
        gone = _parse_iso(str(p[0])) if p[0] else None
        gone = gone or now
        if (now - gone).total_seconds() < _UNIT_RECOVER_HOLD:
            out[name] = [gone.isoformat(), int(p[1] or 1)]
    return out


def units_block(rep: dict) -> dict | None:
    """Блок упавших юнитов из отчета (report.d/units.json, агент отдает как есть)."""
    block = ((rep.get("extras") or {}).get("units")) or {}
    if not isinstance(block, dict) or block.get("v") != 1:
        return None
    ts = float(block.get("ts") or 0)
    ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > _UNITS_MAX_AGE:
        return None
    return block


# Сервис, который systemd перезапускает сам (Restart=), падает и поднимается и никогда не
# бывает failed: растет только счетчик перезапусков (helper units-setup 0.2, "restarting").
# Цикл - три прироста счетчика за 15 минут, как у контейнеров Docker; отбой - 15 минут без
# новых перезапусков.
_UNIT_LOOP_WINDOW = 900
_UNIT_LOOP_MIN = 3


def units_restarting(rep: dict) -> list[dict] | None:
    """Сервисы со счетчиком перезапусков из свежего блока helper'а. None - helper старше 0.2
    или блок протух: судить не по чему."""
    ub = units_block(rep)
    if ub is None or not isinstance(ub.get("restarting"), list):
        return None
    return [u for u in ub["restarting"] if isinstance(u, dict) and u.get("unit")]


def unit_loop_hits(cs: dict | None, now: datetime) -> list[list]:
    """Приросты счетчика перезапусков за окно: [[время, сколько перезапусков], ...]."""
    out = []
    for h in (cs or {}).get("hits") or []:
        if isinstance(h, list) and len(h) == 2:
            t = _parse_iso(str(h[0]))
            if t is not None and (now - t).total_seconds() <= _UNIT_LOOP_WINDOW:
                out.append(h)
    return out


def unit_loop_step(prev: dict | None, n: int, now: datetime) -> dict:
    """Состояние цикла юнита после нового отчета: {"n": счетчик, "hits": приросты за окно,
    "alerted": ушел ли алерт}. Считаем только рост: ручной restart обнуляет счетчик, а первый
    отчет о юните прироста не дает (неизвестно, когда набежало)."""
    p = prev if isinstance(prev, dict) else {}
    hits = unit_loop_hits(p, now)
    last = p.get("n")
    if isinstance(last, int) and n > last:
        hits.append([now.isoformat(), n - last])
    return {"n": n, "hits": hits, "alerted": bool(p.get("alerted"))}


def unit_loop_text(u: dict, hits: list[list]) -> tuple[str, str]:
    """("5 перезапусков", ", последний раз: код 3") для текста алерта и строки на главной."""
    total = sum(int(h[1]) for h in hits)
    why = unit_why(u) if u.get("result") not in (None, "", "success") else ""
    return (f"{total} {_plural(total, 'перезапуск', 'перезапуска', 'перезапусков')}",
            f", последний раз: {why}" if why else "")


def units_muted(s: Server, now: datetime) -> set[str]:
    """Юниты, по которым сейчас не алертят: "не следить" насовсем или на время."""
    keys = {m[len(_UNIT_MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(_UNIT_MUTE_PREFIX)}
    for k, until in (s.alert_snoozes or {}).items():
        u = _parse_iso(until)
        if k.startswith(_UNIT_MUTE_PREFIX) and u is not None and u > now:
            keys.add(k[len(_UNIT_MUTE_PREFIX):])
    return keys


def unit_level(u: dict) -> int:
    """Упавший демон (nginx, postgres) или пропавший маунт - сервис лежит, это проблема.
    Упавшее задание по расписанию (oneshot: certbot, бэкап) - работа не сделана,
    предупреждение."""
    name = str(u.get("unit") or "")
    if name.endswith(".service"):
        return 1 if u.get("type") == "oneshot" else 2
    return 2 if name.endswith((".mount", ".automount", ".swap", ".socket")) else 1


def unit_why(u: dict) -> str:
    """Причина словами: код 1, убит сигналом KILL, не уложился во время."""
    res = str(u.get("result") or "")
    st = u.get("status")
    if res == "exit-code":
        return f"код {st}" if st not in (None, 0) else "ошибка запуска"
    if res == "signal":
        return f"убит сигналом {_SIGNALS.get(st, st)}" if st else "убит сигналом"
    return _UNIT_RESULT.get(res) or res or "упал"


def units_text(units: list[dict], ref: float, limit: int = 4) -> str:
    """Один юнит: "упал certbot.service: код 1, 3 ч назад" и строка лога ниже. Несколько -
    списком, строка лога - у самого серьезного."""
    def ago(u: dict) -> str:
        since = float(u.get("since") or 0)
        if since <= 0:
            return ""
        d = max(0.0, ref - since)
        return (f", {int(d // 86400)} дн назад" if d >= 86400 else f", {int(d // 3600)} ч назад"
                if d >= 3600 else f", {max(1, int(d // 60))} мин назад")
    units = sorted(units, key=lambda u: -unit_level(u))
    if len(units) == 1:
        txt = f"упал {units[0]['unit']}: {unit_why(units[0])}{ago(units[0])}"
    else:
        shown = ", ".join(f"{u['unit']} ({unit_why(u)})" for u in units[:limit])
        more = f" и еще {len(units) - limit}" if len(units) > limit else ""
        txt = f"упали юниты: {shown}{more}"
    line = next((str(x) for x in reversed(units[0].get("log") or []) if x), "")
    if line:
        txt += f"\n↳ {units[0]['unit'] + ': ' if len(units) > 1 else ''}{line[:200]}"
    return txt


def top_eaters(rep: dict, kind: str) -> str:
    """Кто ест ресурс - из снимка top_cpu/top_mem последнего отчёта. Процессы с
    одним именем складываем: у php-fpm полсотни воркеров, по отдельности каждый
    мелкий, и в топе от них толку нет."""
    rows = rep.get("top_cpu" if kind == "cpu" else "top_mem") or []
    by: dict[str, float] = {}
    for p in rows:
        if not isinstance(p, dict):
            continue
        name = str(p.get("comm") or "?").strip() or "?"
        if kind == "cpu":
            by[name] = by.get(name, 0.0) + float(p.get("cpu") or 0)
        else:
            priv = max(float(p.get("rss") or 0) - float(p.get("shared") or 0), 0.0)
            by[name] = by.get(name, 0.0) + priv
    top = sorted(by.items(), key=lambda kv: kv[1], reverse=True)[:_CAUSE_TOP_N]
    if kind == "cpu":
        return ", ".join(f"{n} {v:.0f}%" for n, v in top if v >= _CAUSE_MIN_CPU)
    return ", ".join(f"{n} {_fmt_size(v)}" for n, v in top if v > 0)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _cores(pct: float) -> str:
    """% одного ядра словами: 4510 -> "45 ядер", 250 -> "2.5 ядра"."""
    v = pct / 100
    if v >= 10:
        n = round(v)
        return f"{n} {_plural(n, 'ядро', 'ядра', 'ядер')}"
    return f"{v:.1f} ядра"


# Группа одинаковых процессов одного владельца, которые давно крутят по ядру каждый (агент
# 2.21, cpu_groups[].spin). Одиночный занятой демон (containerd на 1.3 ядра за 84 дня на
# k8s-a-prc) сюда не попадает: нужно хотя бы три одинаковых процесса.
_SPIN_MIN = 3


def pod_uid_names(servers) -> dict[str, str]:
    """Начало uid пода -> ns/имя по отчетам всех нод с доступом к kube-api (агент 2.21+).
    Агент воркера без доступа к kube-api знает свои поды только по uid из cgroup."""
    out: dict[str, str] = {}
    for s in servers:
        for p in (((s.last_report or {}).get("kube") or {}).get("pods") or []):
            if isinstance(p, dict) and p.get("u"):
                out[str(p["u"])] = f"{p.get('ns') or '?'}/{p.get('name') or '?'}"
    return out


def group_owner(g: dict, pod_names: dict[str, str] | None = None) -> str:
    """Владелец группы процессов словами: "под ns/имя", "контейнер x", "юнит x.service"."""
    owner, kind = str(g.get("owner") or ""), g.get("kind")
    if kind == "pod":
        if owner.startswith("pod "):
            owner = (pod_names or {}).get(owner[4:], owner[4:])
        return f"под {owner}"
    if kind == "container":
        return f"контейнер {owner}"
    if kind == "unit":
        return f"юнит {owner}"
    return ""


def spin_groups(rep: dict) -> list[dict]:
    return [g for g in (rep.get("cpu_groups") or [])
            if isinstance(g, dict) and int(g.get("spin") or 0) >= _SPIN_MIN]


def spin_text(groups: list[dict], pod_names: dict[str, str] | None = None, with_age: bool = True) -> str:
    """"47 процессов chrome (под ns/имя) крутят по ядру вхолостую: 45 ядер, старшему 186 дн".
    В "Что сломано" возраст не пишем: там длительность и так стоит в конце строки."""
    parts = []
    for g in groups:
        n, age = int(g.get("spin") or 0), int(g.get("spin_age") or 0)
        own = group_owner(g, pod_names)
        age_t = f"{age // 86400} дн" if age >= 86400 else f"{age // 3600} ч"
        parts.append(f"{n} {_plural(n, 'процесс', 'процесса', 'процессов')} {g.get('comm') or '?'}"
                     + (f" ({own})" if own else "")
                     + f" крутят по ядру вхолостую: {_cores(float(g.get('cpu') or 0))}"
                     + (f", старшему {age_t}" if with_age else ""))
    return "; ".join(parts)


def groups_text(groups: list[dict], pod_names: dict[str, str] | None = None) -> str:
    """"chrome x47 (под ns/имя) 45 ядер, containerd (юнит k0sworker.service) 1.3 ядра"."""
    items = []
    for g in groups[:_CAUSE_TOP_N]:
        cpu = float(g.get("cpu") or 0)
        if cpu < _CAUSE_MIN_CPU:
            continue
        n, own = int(g.get("n") or 1), group_owner(g, pod_names)
        items.append(f"{g.get('comm') or '?'}{f' x{n}' if n > 1 else ''}{f' ({own})' if own else ''} {_cores(cpu)}")
    return ", ".join(items)


# Что из alert_state отдать как "с какого момента проблема": "<вид>_from" ставит цикл алертов
# для любого вида, а у пороговых метрик точнее "<вид>_since" - начало превышения, а не алерта.
_SINCE_KEYS = ("cpu", "mem", "temp", "conntrack", "disktemp", "db_conn", "web_5xx", "clock",
               "backup_missing", "dns", "pressure", "net_health", "kube_node", "link", "traffic")


def alert_since(s: Server) -> dict[str, str]:
    """Только активные проблемы ("<вид>_from" снимается при отбое). "<вид>_since" бывает
    устаревшим - backup_missing_since живет и после того, как бэкап настроили, - поэтому
    берем его, только пока проблема активна и только если он раньше начала алерта."""
    st = s.alert_state or {}
    out: dict[str, str] = {}
    for key, v in st.items():
        if not (key.endswith("_from") and isinstance(v, str) and v):
            continue
        k = key[:-5]
        early = st.get(f"{k}_since") if k in _SINCE_KEYS else None
        out[k] = early if isinstance(early, str) and early and early < v else v
    return out


def docker_since(s: Server) -> dict[str, str]:
    """С какого момента лежит каждый упавший контейнер (alert_state["docker"][имя]["down_since"])."""
    out = {}
    for name, cs in ((s.alert_state or {}).get("docker") or {}).items():
        if isinstance(cs, dict) and isinstance(cs.get("down_since"), str):
            out[name] = cs["down_since"]
    return out


def needed_pod_names(s: Server, names: dict[str, str] | None) -> dict[str, str]:
    """Имена только тех подов, которые этот сервер знает по uid ("pod 56279ecf" в cpu_groups)."""
    if not names:
        return {}
    out = {}
    for g in ((s.last_report or {}).get("cpu_groups") or []):
        own = str(g.get("owner") or "") if isinstance(g, dict) else ""
        if g.get("kind") == "pod" and own.startswith("pod ") and own[4:] in names:
            out[own[4:]] = names[own[4:]]
    return out


async def hour_baseline(session: AsyncSession, server_id: int, now: datetime) -> dict[str, float]:
    """Норма для ЭТОГО времени суток: медиана трафика и TCP-соединений в том же часе
    предыдущих _BASE_DAYS суток (плюс-минус полчаса от текущего времени).

    Час суток важен - у трафика своя суточная кривая, и сравнение со средним за
    неделю объявляло бы наплывом каждый вечерний пик. Берём именно медиану: наплыв,
    который идёт вторые сутки, попадает в окно вчерашнего дня, но медиану семи дней
    не сдвигает. Сегодняшние замеры не берём вовсе - в них уже сам наплыв."""
    windows = [
        and_(ServerMetric.ts >= day - _BASE_HALF, ServerMetric.ts <= day + _BASE_HALF)
        for day in (now - timedelta(days=d) for d in range(1, _BASE_DAYS + 1))
    ]
    rows = list(await session.execute(
        select(ServerMetric.net_rx, ServerMetric.net_tx, ServerMetric.sock_tcp,
               ServerMetric.web_rpm).where(
            ServerMetric.server_id == server_id, or_(*windows)
        )
    ))
    if len(rows) < _BASE_MIN_SAMPLES:
        return {}
    out = {"net": statistics.median([float(r[0] or 0) + float(r[1] or 0) for r in rows])}
    conn = [float(r[2]) for r in rows if r[2] is not None]
    if len(conn) >= _BASE_MIN_SAMPLES:
        out["conn"] = statistics.median(conn)
    rpm = [float(r[3]) for r in rows if r[3] is not None]
    if len(rpm) >= _BASE_MIN_SAMPLES:
        out["rpm"] = statistics.median(rpm)
    return out


def web_rate_field(extras: dict | None, now: datetime, clock_unix: float = 0,
                   field: str = "rpm", max_age: int = 600) -> float | None:
    """Запросов в минуту по всем access-логам ноды. Блок кладёт helper webserver-setup
    (report.d/web-rate.json), агент отдаёт его как есть. Протухший блок игнорируем:
    хелпер мог встать, а вчерашний поток выглядел бы как сегодняшний.

    Возраст меряем по часам САМОЙ ноды (clock_unix из отчёта): её часы бывают сдвинуты
    относительно панели - на это есть отдельный алерт, - и живые данные из-за сдвига
    терялись бы. Часов ноды нет (старый агент) - сверяемся со своими."""
    block = ((extras or {}).get("web-rate")) or {}
    if not isinstance(block, dict):
        return None
    ts = float(block.get("ts") or 0)
    ref = float(clock_unix or 0) or now.timestamp()
    if ts <= 0 or ref - ts > max_age:
        return None
    v = block.get(field)
    return float(v) if isinstance(v, (int, float)) else None


def web_rate_total(extras: dict | None, now: datetime, clock_unix: float = 0,
                   max_age: int = 600) -> float | None:
    """Запросов в минуту по всем логам ноды."""
    return web_rate_field(extras, now, clock_unix, "rpm", max_age)


def web_5xx_total(extras: dict | None, now: datetime, clock_unix: float = 0,
                  max_age: int = 600) -> float | None:
    """Ответов 5xx в минуту по всем логам ноды. Считает тот же helper: код ответа он
    берёт из той же строки лога, которую и так прочитал для счёта запросов."""
    return web_rate_field(extras, now, clock_unix, "e5", max_age)


def _times(cur: float, base: float, floor: float) -> str:
    """«x40», если сейчас во столько раз выше нормы. Пусто, если нормы нет, поток
    сам по себе мелкий или превышение в пределах _CAUSE_RATIO."""
    if base <= 0 or cur < floor or cur / base < _CAUSE_RATIO:
        return ""
    return f"x{min(cur / base, 999):.0f}"


def cause_text(rep: dict, kind: str, base: dict[str, float], now: datetime | None = None,
               pod_names: dict[str, str] | None = None) -> str:
    """Хвост к тексту алерта: кто ест ресурс и не наплыв ли это. Пусто, если
    сказать нечего - тогда алерт выглядит как раньше."""
    parts = []
    # CPU у агентов 2.21+ - группами с владельцем: в топе всего 8 процессов, и 47 зависших
    # chrome выглядели бы как "chrome 800%", а не 45 ядер одного пода
    groups = [g for g in (rep.get("cpu_groups") or []) if isinstance(g, dict)] if kind == "cpu" else []
    eaters = groups_text(groups, pod_names) if groups else top_eaters(rep, kind)
    if eaters:
        # «сверху» читалось непонятно: это те, кто больше всего ест ресурс
        parts.append(f"больше всего {'CPU' if kind == 'cpu' else 'памяти'} у {eaters}")
    if kind == "mem":
        # процессы контейнера в топе не подписаны, а перезапускают контейнер (агент 2.14+)
        conts = sorted(
            ((str(c.get("name") or "?"), float(c.get("mem") or 0))
             for c in ((rep.get("docker") or {}).get("containers") or [])
             if isinstance(c, dict) and c.get("mem")),
            key=lambda x: -x[1],
        )[:2]
        if conts:
            parts.append("контейнеры: " + ", ".join(f"{n} {_fmt_size(m)}" for n, m in conts))
        # и поды Kubernetes этой ноды (агент 2.16+): у k8s-нод docker пустой, а память
        # держат поды, и перезапускают их деплоймент
        pods = sorted(
            ((f"{p.get('ns') or '?'}/{p.get('name') or '?'}", float(p.get("mem") or 0))
             for p in ((rep.get("kube") or {}).get("pods") or [])
             if isinstance(p, dict) and p.get("mem")),
            key=lambda x: -x[1],
        )[:2]
        if pods:
            parts.append("поды: " + ", ".join(f"{n} {_fmt_size(m)}" for n, m in pods))
    surge = []
    # Запросы точнее байтов: 2.3 млн редиректов по 300 байт канал почти не шевелят.
    # Их считает helper webserver-setup; нет его - остаются трафик и соединения.
    cur_rpm = web_rate_total(rep.get("extras"), now, rep.get("clock_unix") or 0) if now else None
    rpm = _times(cur_rpm or 0, base.get("rpm") or 0, _SURGE_MIN_RPM) if cur_rpm else ""
    if rpm:
        surge.append(f"запросов к веб-серверу {rpm}")
    else:
        net = _times(float(rep.get("net_rx") or 0) + float(rep.get("net_tx") or 0),
                     base.get("net") or 0, _SURGE_MIN_NET)
        if net:
            surge.append(f"трафик {net}")
        conn = _times(float(rep.get("sock_tcp") or 0), base.get("conn") or 0, _SURGE_MIN_CONN)
        if conn:
            surge.append(f"соединений {conn}")
    if surge:
        parts.append(" и ".join(surge) + " к обычному для этого часа")
    return " - " + "; ".join(parts) if parts else ""


# Метрики, которые умеют ходить вокруг порога. Наплыв трафика держится сутками, CPU
# то выше порога, то ниже, и на каждый заход прилетала пара «сработало - отбой».
# После _FLAP_METRIC_LIMIT заходов за окно шлём одно сообщение и замолкаем.
# Эскалации (диск warn -> crit) не считаем: там растёт проблема, а не мигает метрика.
_FLAP_METRIC_KINDS = frozenset({"cpu", "mem", "disk", "temp", "conntrack", "db_conn", "disktemp",
                                "web_5xx"})
_FLAP_METRIC_WINDOW = 6 * 3600

# Отбой "сервер задыхается", сети и медленного диска - только после часа спокойствия. Ночной
# pg_dump на одной ноде дал три пары "сработало - отбой" за два часа: диск то не успевал, то
# успевал, а история одна.
_CALM_S = 3600
_TRAFFIC_CALM_S = 12 * 3600
# Под в CrashLoopBackOff между падениями на минуту становится Running: без выдержки отбоя
# приходило "снова в норме" посреди цикла падений.
_POD_CALM_S = 900
# Репозиторий чартов недоступен: развернутое работает, не придут только новые версии чартов.
# Час-другой сети до репозитория нет - это не повод писать (три пары за неделю на одной ноде).
_FLUX_STALE_HOLD = 2 * 3600

# Гистерезис CPU и RAM: алерт снимается, только когда значение ниже порога на _HYST пунктов и
# держится там выдержку. Нода, которая каждый день сидит на 80-100% CPU, переходила 90%, через
# 15 минут получала "снова в норме: 89%", а через час новый алерт: до восьми сообщений в день о
# том, что сервер просто загружен.
_HYST = 10
_FLAP_METRIC_LIMIT = 3



# Ошибки 5xx по логам веб-сервера. Синтетический монитор такую долю не видит: 0.1-0.3%
# это один запрос из трёхсот. Окно 15 минут - и выдержка, и знаменатель сразу. Нижний
# порог по числу ошибок страхует мелкие ноды: на ноде с тысячей запросов в час одна
# случайная 503 - это уже 0.1%, и будить из-за неё нельзя.
_WEB_ERR_WINDOW = timedelta(minutes=15)
_WEB_ERR_MIN = 20           # ошибок за окно, меньше - не повод
_WEB_ERR_MIN_POINTS = 10    # минут с данными в окне, меньше - окно не набралось
# Минут с ошибками в окне. Живой случай 26.09.2026: деплой панели на минуту роняет бэкенд,
# фронт отвечает 502 на запросы агентов - 100-130 ошибок за одну минуту, и алерт уходил на
# каждый деплой, а через час отбой. Рестарт дает минуту-две ошибок и проходит сам, а
# поломка, на которую надо реагировать, идет дольше.
_WEB_ERR_MIN_MINUTES = 3

# Пробы сканеров: пути, которые живой посетитель не запрашивает никогда. Служебные файлы с
# точкой в начале (/.env, /.git/config, /.aws/credentials), чтение файлов через dev-сервер
# Vite (/@fs/...), выход за корень сайта, известные эксплойты. Приложение иногда отвечает на
# них 500 вместо 404, и пачки от сканеров давали алерт: 07.10.2026 на stand-a dreamsite-nginx
# отдал 22 ответа 500 на /@fs/.env и соседей при 30 тыс. запросов (0.07%), каждая пачка с
# одного адреса под видом Googlebot и GPTBot. Сайт при этом работал.
_PROBE_PATH = re.compile(
    r"(?:^|/)\.(?!well-known(?:/|$))[^/]"  # точка в начале части пути, кроме /.well-known/
    r"|/@fs/"
    r"|\.\./|%2e%2e|\.\.%2f"
    r"|/vendor/phpunit/"
    r"|/etc/passwd",
    re.I,
)


def probe_only(paths: list | None) -> bool:
    """Все пути с ошибками минуты - пробы сканера. Helper присылает пять самых частых путей:
    если среди них есть хоть один настоящий, минута считается целиком, как раньше."""
    items = [x for x in (paths or []) if isinstance(x, dict) and x.get("p")]
    return bool(items) and all(_PROBE_PATH.search(str(x["p"])) for x in items)


# Выкладка. Пока docker пересоздает контейнер, nginx перед ним отдает 502: имя не
# резолвится, порт еще не слушают, старый контейнер рвет соединения. Это секунды, и чинить
# после них нечего. Живой случай 19.10.2026: две выкладки панели за восемь минут дали три
# минуты с ошибками (одна пачка в 15 секунд легла на две минуты helper'а), и
# _WEB_ERR_MIN_MINUTES не спас - ушел алерт "1.6% запросов закончились ошибкой". Минута не
# считается сбоем, если ее 5xx почти все - отказы связи с контейнером, который выкатили в
# эти минуты: в хронологии сервера новый образ, "перезапущен или пересоздан", новый или
# удаленный контейнер. Упал и поднялся сам (crash) - не выкладка, такие минуты считаются,
# а падения подряд ловят docker_loop и docker_restarts.
_RESTART_KINDS = frozenset({"refused", "resolve", "reset", "closed", "nofile", "unreach", "ctimeout", "nolive"})
_DEPLOY_KINDS = ("image", "restart", "new", "gone")
_DEPLOY_NEAR = timedelta(minutes=3)
_DEPLOY_SHARE = 0.8
# адрес контейнера в сети docker: после пересоздания его уже нет в docker ps, и helper не
# может назвать контейнер по адресу
_DOCKER_IP = re.compile(r"(10|172\.(1[6-9]|2\d|3[01])|192\.168)(\.\d{1,3}){2,3}(:\d+)?")


def _upstream_is(it: dict, obj: str) -> bool:
    """Upstream из error-лога - этот контейнер? По имени, которое дал helper, или по имени из
    конфига nginx: backend - это и backend, и kervax-backend-1 (compose: проект-сервис-номер).
    Адрес сети docker без имени - контейнер, которого уже нет, считаем выкаченным."""
    c = str(it.get("c") or "")
    if c:
        return c == obj
    u = str(it.get("u") or "")
    if _DOCKER_IP.fullmatch(u):
        return True
    host = u.split(":", 1)[0]
    return bool(host) and (host == obj or re.fullmatch(rf".+[-_]{re.escape(host)}[-_]\d+", obj) is not None)


def restart_blip(ups, e5, ts: datetime, deploys: list[tuple[datetime, str]] | None) -> bool:
    """5xx минуты - это выкладка: не меньше 80% из них - отказы связи с контейнером, который
    выкатили в пределах трех минут от нее (deploys - события хронологии: время, контейнер)."""
    if not deploys or not isinstance(ups, list) or not e5:
        return False
    near = [obj for t, obj in deploys if obj and abs(t - _aware(ts)) <= _DEPLOY_NEAR]
    if not near:
        return False
    n = sum(int(it.get("n") or 0) for it in ups
            if isinstance(it, dict) and it.get("k") in _RESTART_KINDS and any(_upstream_is(it, o) for o in near))
    return n >= _DEPLOY_SHARE * int(e5)


async def web_deploys(session: AsyncSession, since: datetime,
                      server_id: int | None = None) -> dict[int, list[tuple[datetime, str]]]:
    """Выкладки контейнеров с since минус три минуты: {server_id: [(время, контейнер)]}."""
    q = select(ServerEvent.server_id, ServerEvent.ts, ServerEvent.obj).where(
        ServerEvent.ts >= since - _DEPLOY_NEAR, ServerEvent.cat == "deploy", ServerEvent.kind.in_(_DEPLOY_KINDS))
    if server_id is not None:
        q = q.where(ServerEvent.server_id == server_id)
    out: dict[int, list[tuple[datetime, str]]] = {}
    for sid, ts, obj in await session.execute(q):
        out.setdefault(sid, []).append((_aware(ts), obj or ""))
    return out


async def web_error_window(session: AsyncSession,
                           now: datetime) -> dict[int, tuple[float, float, int, dict, float]]:
    """{server_id: (запросов, ошибок 5xx, минут с данными, {ключ лога: {минута: ошибок}},
    ошибок не в счет)} за последние 15 минут. Три запроса на тик, а не по запросу на ноду.
    Разбивка по логам - чтобы вычесть заглушенные и посчитать минуты с ошибками без них.
    Минуты, где ошибки только на пробах (probe_only) или от выкладки (restart_blip), в
    разбивку не попадают, их ошибки идут отдельной суммой: ее вычитают из общего числа так
    же, как заглушенные."""
    since = now - _WEB_ERR_WINDOW
    rows = await session.execute(
        select(ServerMetric.server_id, func.sum(ServerMetric.web_rpm),
               func.sum(ServerMetric.web_5xx), func.count())
        .where(ServerMetric.ts >= since, ServerMetric.web_5xx.is_not(None))
        .group_by(ServerMetric.server_id)
    )
    deploys = await web_deploys(session, since)
    # Минуты с ошибками - по минутам helper'а (src_ts), а не по точкам истории: одна и та
    # же минута helper'а может попасть в две точки подряд.
    per: dict[int, dict[str, dict[int, int]]] = {}
    probes: dict[int, dict[tuple[str, int], int]] = {}
    for sid, label, log, src, e5, paths, ups, ts in await session.execute(
        select(WebErrorSample.server_id, WebErrorSample.label, WebErrorSample.log,
               WebErrorSample.src_ts, WebErrorSample.e5, WebErrorSample.paths, WebErrorSample.ups,
               WebErrorSample.ts)
        .where(WebErrorSample.ts >= since, WebErrorSample.e5 > 0)
    ):
        key = web_label_key(label or log)
        if probe_only(paths) or restart_blip(ups, e5, ts, deploys.get(sid)):
            probes.setdefault(sid, {})[(key, int(src))] = int(e5 or 0)
            continue
        per.setdefault(sid, {}).setdefault(key, {})[int(src)] = int(e5 or 0)
    return {sid: (float(t or 0), float(e or 0), int(n or 0), per.get(sid, {}),
                  float(sum((probes.get(sid) or {}).values())))
            for sid, t, e, n in rows}


def web_error_level(total: float, errs: float, points: int, threshold: float,
                    minutes: int | None = None) -> bool:
    """Пошли ли ошибки: окно набралось, ошибок больше случайных, их доля выше порога и шли
    они не одну-две минуты (minutes - минут с ошибками; None - не проверять)."""
    if not threshold or points < _WEB_ERR_MIN_POINTS or total <= 0:
        return False
    if minutes is not None and minutes < _WEB_ERR_MIN_MINUTES:
        return False
    return errs >= _WEB_ERR_MIN and errs / total * 100 >= threshold


def web_log_label(entry: dict) -> str:
    """Как назвать лог человеку: под kubernetes, контейнер с доменами в скобках, домены,
    имя контейнера или файла. Путь json-лога докера - это хеш, он ни о чем не говорит.

    Несколько доменов в одной подписи - это один лог: nginx пишет запросы всех своих
    server_name в общий access-лог, и разделить их по сайтам можно, только если в формате
    лога есть $host. Имя контейнера впереди объясняет, откуда такая группа. "+N", а не
    "и еще N": подпись попадает и в легенду графика английского интерфейса."""
    name = str(entry.get("name") or "")
    sites = [str(x) for x in (entry.get("sites") or []) if x]
    if "/" in name:
        # под kubernetes - по контроллеру (имя пода меняется с каждым выкатом), с
        # доменами из его nginx, если helper их нашел (у ingress-nginx - хосты Ingress)
        name = _pod_controller(name)
        if not sites:
            return name
    if sites:
        doms = ", ".join(sites[:2]) + (f" +{len(sites) - 2}" if len(sites) > 2 else "")
        return f"{name} ({doms})" if name else doms
    return name or str(entry.get("log") or "?").split("/")[-1]


def web_label_key(label: str) -> str:
    """По чему сводить записи одного лога. Ключ лога у контейнера менялся (путь json-лога
    до хелпера 0.13, docker:имя после), подпись тоже ("домены и еще N" раньше, "контейнер
    (домены +N)" теперь), и один nginx показывался двумя строками. Общее у них - домены."""
    s = label.strip()
    m = re.fullmatch(r"([^()]*) \((.+)\)", s)
    head = m.group(1) if m else s
    # Под kubernetes - по его контроллеру: имя пода меняется при каждом выкате
    # (ns/app-5c67cd68dd-djg5j), и без этого строки одного деплоймента множились, а
    # заглушенный лог снова начинал алертить после первого же рестарта. Домены пода
    # (хосты Ingress у ingress-nginx) в ключ не идут: их список меняется чаще.
    if "/" in head:
        return _pod_controller(head)
    if m:
        s = m.group(2)
    return re.sub(r" и ещё (\d+)$", r" +\1", s)


_K8S_RAND = "[bcdfghjklmnpqrstvwxz2456789]"


def _pod_controller(name: str) -> str:
    """ns/app-5c67cd68dd-djg5j -> ns/app. Хвосты пода kubernetes берет из алфавита без
    гласных, поэтому живые слова (ns/web-12345, ns/postgres-0) не срезаются."""
    m = re.fullmatch(rf"([a-z0-9.-]+/.+?)(?:-{_K8S_RAND}{{6,10}})?-{_K8S_RAND}{{5}}", name.strip())
    return m.group(1) if m else name.strip()

# Заглушить 5xx одного лога (домена): ключ web_5xx:<ключ лога> в alert_snoozes (на время)
# или alert_mutes (насовсем). Заглушенный лог выпадает из расчета: ошибки прочих логов
# ноды алертят как раньше. Так боты, которым приложение отдает 500 на robots.txt, не
# держат в тишине весь сервер.
_WEB_MUTE_PREFIX = "web_5xx:"


def web_muted_keys(s: Server, now: datetime) -> set[str]:
    """Ключи логов, по которым 5xx сейчас не алертят."""
    keys = {m[len(_WEB_MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(_WEB_MUTE_PREFIX)}
    for k, until in (s.alert_snoozes or {}).items():
        u = _parse_iso(until)
        if k.startswith(_WEB_MUTE_PREFIX) and u is not None and u > now:
            keys.add(k[len(_WEB_MUTE_PREFIX):])
    return keys


def web_breakdown(extras: dict | None, now: datetime, clock_unix: float = 0,
                  top: int = 5) -> tuple[list | None, list | None]:
    """Для графиков стеком: самые нагруженные логи минуты и ошибки 5xx по кодам (сумма
    по всем логам). (None, None), если блока нет или он протух."""
    if web_rate_total(extras, now, clock_unix) is None:
        return None, None
    logs = [x for x in ((extras or {}).get("web-rate") or {}).get("logs") or [] if isinstance(x, dict)]
    # Полоса графика - по подписи, и одинаковые подписи складываются. Поды одного
    # деплоймента - одна полоса (имя пода меняется с каждым выкатом, и полоса менялась бы
    # вместе с ним), два контейнера одного приложения Coolify - тоже.
    agg: dict[str, list[int]] = {}
    for x in logs:
        name = web_log_label(x)
        if "/" in name and "(" not in name:
            name = web_label_key(name)
        rpm, e5 = int(x.get("rpm") or 0), int(x.get("e5") or 0)
        # Общий лог нескольких сайтов, у которого helper (0.21) разложил поток по доменам
        # (в формате есть $host): полоса на каждый сайт. Что не разложилось - под подписью лога.
        for h in (x.get("hosts") or []):
            if not isinstance(h, dict) or not h.get("h") or h.get("h") == "-":
                continue
            hr, he = int(h.get("rpm") or 0), int(h.get("e5") or 0)
            slot = agg.setdefault(str(h["h"])[:120], [0, 0])
            slot[0] += hr
            slot[1] += he
            rpm, e5 = rpm - hr, e5 - he
        if rpm <= 0 and e5 <= 0 and x.get("hosts"):
            continue
        slot = agg.setdefault(name[:120], [0, 0])
        slot[0] += max(rpm, 0)
        slot[1] += max(e5, 0)
    busiest = sorted(((k, r, e) for k, (r, e) in agg.items() if r > 0), key=lambda t: -t[1])
    tops = [{"k": k, "r": r, "e": e} for k, r, e in busiest[:top]]
    codes: dict[str, float] = {}
    for x in logs:
        e5 = int(x.get("e5") or 0)
        if e5 <= 0:
            continue
        c5 = x.get("c5") if isinstance(x.get("c5"), dict) else {}
        got = sum(int(n or 0) for n in c5.values())
        if got <= 0:  # хелпер до 0.12 кодов не присылает
            codes["5xx"] = codes.get("5xx", 0) + e5
            continue
        # На большом потоке helper считает выборку и домножает число ошибок, а коды - нет
        # (они про то, какие ошибки). Растягиваем коды до числа ошибок, иначе полосы по
        # кодам не доставали бы до общей линии.
        for c, n in c5.items():
            codes[str(c)] = codes.get(str(c), 0) + int(n or 0) * e5 / got
    return tops, [{"c": c, "n": round(n, 1)} for c, n in sorted(codes.items())]


# Ошибки кончились - но это ещё не отбой. У web-a 5xx шли пачками: 15 минут
# есть, 15 нет, и за 45 минут пришло две пары «ошибки - снова в норме». Отбой только
# после часа подряд ниже порога: пачки внутри часа - это одна история, а не четыре.
_WEB_ERR_CLEAR = timedelta(hours=1)


async def web_error_where(session: AsyncSession, server_id: int, now: datetime,
                          muted: set[str] | frozenset = frozenset()) -> dict:
    """Где ошибки за окно алерта: логи по убыванию, коды, частые пути. Берём из минут с
    ошибками, а не из последнего отчёта: ошибки могли кончиться минуту назад, а за
    окно их было сорок, и алерт без «где» (так было в первом сообщении) бесполезен."""
    rows = list(await session.scalars(
        select(WebErrorSample).where(WebErrorSample.server_id == server_id,
                                     WebErrorSample.ts >= now - _WEB_ERR_WINDOW)
    ))
    deploys = (await web_deploys(session, now - _WEB_ERR_WINDOW, server_id)).get(server_id)
    by_log: dict[str, list] = {}
    codes: dict[str, int] = {}
    paths: dict[str, int] = {}
    hosts: dict[str, int] = {}
    ups: dict = {}  # почему 502/504: до какого upstream nginx не достучался (helper 0.26)
    for r in sorted(rows, key=lambda x: x.ts):
        if web_label_key(r.label or r.log) in muted:
            continue  # заглушенный лог: про него не пишем, даже если он и шумит
        if probe_only(r.paths):
            continue  # минута, где 500 только сканерам: в алерт она не входит
        if restart_blip(getattr(r, "ups", None), r.e5, r.ts, deploys):
            continue  # секунды выкладки контейнера: тоже не сбой
        acc = by_log.setdefault(web_label_key(r.label or r.log), [r.label, 0])
        acc[0] = r.label or acc[0]  # подпись - самая свежая
        acc[1] += int(r.e5 or 0)
        for c, n in (r.codes or {}).items():
            codes[str(c)] = codes.get(str(c), 0) + int(n or 0)
        for it in (r.paths or []):
            if isinstance(it, dict) and it.get("p"):
                paths[str(it["p"])] = paths.get(str(it["p"]), 0) + int(it.get("n") or 0)
        for it in (r.hosts or []):
            if isinstance(it, dict) and it.get("h") and it.get("h") != "-":
                hosts[str(it["h"])] = hosts.get(str(it["h"]), 0) + int(it.get("n") or 0)
        web_upstream.add(ups, getattr(r, "ups", None))
    logs = sorted(by_log.values(), key=lambda x: -x[1])
    return {"logs": logs, "codes": codes, "paths": paths, "hosts": hosts, "ups": ups}


def web_where_text(where: dict) -> str:
    """"Больше всего: nginx (form.shop.example, mobile.shop.example +2) - 41. Коды: 502 - 30, 504 - 11. Чаще всего
    падает: /api/v1/form - 38.» Каждый кусок - только если данные есть: коды и пути
    шлёт helper с 0.12."""
    out = []
    logs = [x for x in where.get("logs") or [] if x[1] > 0]
    if logs:
        top = ", ".join(f"{label} - {n}" for label, n in logs[:2])
        more = f" и ещё {len(logs) - 2} лог(а)" if len(logs) > 2 else ""
        out.append(f"больше всего: {top}{more}")
    # общий лог нескольких сайтов, разложенный по доменам (helper 0.21): какой сайт падает
    hosts = sorted((where.get("hosts") or {}).items(), key=lambda kv: -kv[1])
    if hosts:
        out.append("по доменам: " + ", ".join(f"{h} - {n}" for h, n in hosts[:3]))
    codes = sorted((where.get("codes") or {}).items(), key=lambda kv: -kv[1])
    if codes:
        out.append("коды: " + ", ".join(f"{c} - {n}" for c, n in codes[:4]))
    # почему 502/504 (helper 0.26): за причиной больше не надо идти в error-лог на сервер
    why = web_upstream.text(where.get("ups") or {})
    if why:
        out.append("почему: " + why)
    paths = sorted((where.get("paths") or {}).items(), key=lambda kv: -kv[1])
    if paths:
        out.append("чаще всего падает: " + ", ".join(f"{p} - {n}" for p, n in paths[:2]))
    return ". " + ". ".join(s[0].upper() + s[1:] for s in out) if out else ""


def _flapping(s: Server, st: dict, now: datetime, apply, note: list, url: str = "") -> bool:
    """True, если про это переключение писать НЕ нужно. Побочно ведёт счётчик и,
    один раз на серию, кладёт в note сообщение о нестабильности.

    url приходит ПАРАМЕТРОМ: srv_url — вложенная функция цикла алертов, отсюда она не
    видна. Обращение к ней по имени роняло весь проход NameError'ом ровно на пороге
    _FLAP_LIMIT, и панель переставала слать любые серверные алерты."""
    first = _parse_iso(st.get("flap_since") or "") or now
    cnt = int(st.get("flap_count", 0))
    if (now - first).total_seconds() > _FLAP_WINDOW:  # окно истекло — считаем заново
        first, cnt = now, 0
    cnt += 1
    apply(s.id, "flap_since", first.isoformat())
    apply(s.id, "flap_count", cnt)
    if cnt < _FLAP_LIMIT:
        return False  # редкие переключения — обычные алерты, всё как раньше
    if not st.get("flap_muted"):
        apply(s.id, "flap_muted", 1)
        note.append(
            _server_alert_text(
                "offline", s.name,
                f"связь нестабильна: {cnt} обрыва(ов) за {_FLAP_WINDOW // 60} мин — "
                "дальше молчу, пока не устаканится",
                url, group=s.group_name or "",
            )
        )
    return True



def _flap_metric(s: Server, st: dict, now: datetime, apply, note: list, url: str,
                 key: str, title: str) -> bool:
    """True, если про это срабатывание писать НЕ нужно: метрика ходит вокруг порога.
    Устроено как гаситель мигания у обрывов связи, но окно длиннее: с выдержкой в
    15 минут один и тот же порог чаще раза в пару часов не пересечь."""
    pre = f"flap_{key}"
    first = _parse_iso(st.get(f"{pre}_since") or "") or now
    cnt = int(st.get(f"{pre}_count", 0))
    if (now - first).total_seconds() > _FLAP_METRIC_WINDOW:  # окно истекло - считаем заново
        first, cnt = now, 0
    cnt += 1
    apply(s.id, f"{pre}_since", first.isoformat())
    apply(s.id, f"{pre}_count", cnt)
    if cnt < _FLAP_METRIC_LIMIT:
        return False
    if not st.get(f"{pre}_muted"):
        apply(s.id, f"{pre}_muted", 1)
        note.append(_server_alert_text(
            key, s.name,
            f"{title} ходит вокруг порога: {cnt} срабатывания за "
            f"{_FLAP_METRIC_WINDOW // 3600} ч - дальше молчу, пока не устаканится",
            url, group=s.group_name or "",
        ))
    return True


def queue_key(source: str, q: dict) -> str:
    """Ключ очереди: источник обязателен — на ноде бывает несколько инстансов
    RabbitMQ (dev/stage), и имена очередей в них совпадают."""
    vh = (q.get("vhost") or "/").strip()
    return f"{source}|{vh}/{q.get('name') or ''}"


def queue_depth(q: dict) -> int:
    """Глубина = неразобранные + взятые, но не подтверждённые: залипший консьюмер
    держит сообщения в unacked, и по одному ready проблема не видна."""
    return int(q.get("ready") or 0) + int(q.get("unacked") or 0)


def queue_threshold(s: Server, key: str) -> int:
    """Порог для очереди: переопределение важнее общего по ноде. 0 = не алертить."""
    over = s.queue_alert_over or {}
    if key in over:
        try:
            return max(int(over[key]), 0)
        except (TypeError, ValueError):
            return 0
    return max(int(s.queue_alert_depth or 0), 0)


def _fallback_rule(key: str) -> dict:
    """Правило для вида алерта, которого нет в реестре SERVER_ALERT_KINDS.

    Раньше отсутствие правила означало `continue` — новое условие, забытое в
    реестре, НИКОГДА не срабатывало, и заметить это можно было только по факту
    пропущенной аварии. Сырой текст в ленте виден сразу и чинится за минуту,
    а молчание не видно вообще — поэтому по умолчанию всё-таки шлём."""
    log.warning("алерт «%s» не описан в SERVER_ALERT_KINDS — шлём сырым текстом", key)
    return {
        "enabled": True,
        "text": key + ": {value} (порог {threshold})",
        "scope_type": "all",
        "scope": [],
    }


def db_health_problems(s: Server, rep: dict, now: datetime) -> list[tuple[int, str, str]]:
    """Проблемы баз; отказы в подключении (MySQL, dbstat-setup 0.11) у экземпляра, по которому уже горит
    алерт коннектов, не повторяем: та же беда, строка "занято N из M" о ней уже говорит"""
    dh = db_health.problems(rep, now)
    thr = getattr(s, "db_conn_alert_percent", 0) or 0
    worst = db_conn_worst(rep) if thr else None
    if worst is None or worst[0] < thr:
        return dh
    drop = f"{worst[1].get('engine')}:{worst[1].get('container') or ''}:conn"
    return [p for p in dh if p[2] != drop]


def db_conn_worst(rep: dict) -> tuple[float, dict] | None:
    """Самый нагруженный по коннектам движок ноды: (процент занятых, блок СУБД) или None."""
    worst = None
    for db in rep.get("db_stats") or []:
        limit = db.get("conn_max") or 0
        if limit <= 0:  # движок не отдал лимит — считать процент не из чего
            continue
        pct = (db.get("conn_used") or 0) / limit * 100
        if worst is None or pct > worst[0]:
            worst = (pct, db)
    return worst


def kube_warn_days(s: Server) -> list[int]:
    """Пороги сроков кластера в днях, от дальнего к ближнему; пусто - сроки не сторожим."""
    return sorted({int(d) for d in (s.kube_expiry_warn_days or []) if int(d) > 0}, reverse=True)


def kube_expiry_soon(rep: dict, warn_days: list[int],
                     now: datetime) -> tuple[float, float, dict, int] | None:
    """Самый ранний срок кластера в пределах дальнего порога: (истекает, дней осталось,
    запись helper'а, сколько всего сроков в пределах порога) или None."""
    soon, near = None, 0
    for it in rep.get("kube_expiry") or []:
        exp = it.get("expires") or 0
        if exp <= 0:
            continue
        days = (exp - now.timestamp()) / 86400
        if days > warn_days[0]:
            continue
        near += 1
        if soon is None or exp < soon[0]:
            soon = (exp, days, it)
    return (*soon, near) if soon is not None else None


def kube_expiry_ctx(exp: float, days: float, it: dict, near: int) -> dict:
    """Контекст текста про истекающий срок: что, где, когда и что делать."""
    # К ближайшему, а не вниз: «через 4 дн.» при остатке 4 дня 23 часа —
    # формально верно и практически обман, до срока почти пять дней.
    left = round(days)
    phrase = (f"истекает через {left} дн." if left >= 1
              else "истекает сегодня" if days >= 0
              else f"ИСТЁК {abs(left) or 1} дн. назад")
    # Уточнение из хелпера показываем, только если его нет в самом пути:
    # у сертификата note — это имя файла, и «server.crt (server)» — шум.
    note, where = it.get("note") or "", it.get("where") or ""
    if note in ("tls.crt", "config"):
        note = ""  # «TLS-сертификат … (tls.crt)» — повтор самого себя
    if note and note not in where:
        where = f"{where} ({note})"
    kind = it.get("kind") or ""
    advice = _KUBE_ADVICE.get((kind, days < 0), "")
    return {
        "value": phrase,
        "what": _KUBE_KIND.get(kind, "срок"),
        "where": where,
        "advice": f" · {advice}" if advice else "",
        # не для текста, а для иконки: «истекает через 6 дн.» и «ИСТЁК» —
        # разные новости, и в ленте они должны различаться с первого взгляда
        "expired": days < 0,
        "date": datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%d.%m.%Y"),
        "more": f" (и ещё {near - 1} на этом сервере)" if near > 1 else "",
    }


def flux_state(rep: dict) -> tuple[str, dict] | None:
    """Сломанный Flux: ("flux_down" или "flux_stale", контекст текста) или None."""
    broken = [f for f in (rep.get("flux") or [])
              if not f.get("ready") and (f.get("reason") or "") not in _FLUX_TRANSIENT]
    if not broken:
        return None
    # Корень, а не первый по алфавиту. Одна упавшая сборка тянет за собой все
    # зависимые, и те отвечают «DependencyNotReady» — это следствие. В ленте
    # висело именно оно, и инженеру приходилось самому искать, что же упало.
    roots = [f for f in broken if (f.get("reason") or "") != "DependencyNotReady"]
    waiting = len(broken) - len(roots)
    pool = roots or broken
    f = sorted(pool, key=lambda x: (x.get("kind") or "", x.get("where") or ""))[0]
    reason = f.get("reason") or "Ready=False"
    why, hint = _FLUX_REASON.get(reason, ("", ""))
    where = f.get("where") or ""
    ns, _, name = where.partition("/")
    # текст ошибки точнее reason: "проверьте секрет" при недоступном адресе увел бы не туда
    hint = _flux_msg_hint(f.get("message") or "") or hint
    if hint:
        hint = hint.format(ns=ns or "namespace", name=name or "имя")
    tail = []
    if len(pool) > 1:
        tail.append(f"ещё {len(pool) - 1} сломано")
    if waiting:
        tail.append(f"{waiting} ждут этого")
    ctx = {
        "what": f.get("kind") or "ресурс Flux",
        "where": where,
        # человеческая причина, а сырой reason — в скобках: по нему гуглят
        "reason": f"{why} ({reason})" if why else reason,
        "message": _flux_detail(f.get("message") or "") or "без пояснения",
        "more": f" [{', '.join(tail)}]" if tail else "",
    }
    # Упали только репозитории чартов, а все сборки и релизы в Ready: развернутое
    # работает на закешированном чарте, не придут лишь новые версии чартов. Это
    # предупреждение, а не "доставка встала": изменения из Git по-прежнему
    # применяются. Упавший GitRepository, наоборот, останавливает все выкаты.
    if all((x.get("kind") or "") == "HelmRepository" for x in broken):
        return "flux_stale", dict(ctx, hint=f"\n{hint}" if hint else "")
    return "flux_down", dict(ctx, hint=f"\n↳ {hint}" if hint else "")


def web_5xx_now(s: Server, web_err: tuple[float, float, int, dict, float] | None,
                now: datetime) -> tuple[bool, float, float, float] | None:
    """Пошли ли ошибки 5xx за окно в 15 минут: (да или нет, ошибок, запросов, порог %).
    Без заглушенных логов и ответов сканерам. None - порога нет или не по чему судить."""
    thr5 = float(getattr(s, "web_5xx_alert_percent", 0) or 0)
    if not thr5 or web_err is None:
        return None
    total, errs, points, per_log, probe_errs = web_err
    muted = web_muted_keys(s, now)
    if muted:
        errs = max(0.0, errs - sum(sum(m.values()) for k, m in per_log.items() if k in muted))
    # ответы сканерам на /.env, /@fs/ и подобное и секунды выкладки контейнера: сайт не сломан
    errs = max(0.0, errs - probe_errs)
    err_minutes = len({src for k, m in per_log.items() if k not in muted for src in m})
    return web_error_level(total, errs, points, thr5, err_minutes), errs, total, thr5


def dump_issues(rep: dict, now: datetime) -> tuple[list[str], str, list[str], int]:
    """Дампы СУБД так, как их видят алерты backup_dump и backup_dump_space: (сломанные, почему,
    пропущенные из-за места, % свободного). Упал сам бэкап - дампы не судим, это backup_failed."""
    bk = rep.get("backup") or {}
    from_systemd = bk.get("ts_source") == "systemd"
    if bk.get("metric_present") or from_systemd:
        failed = ((bk.get("service_result") or "").strip() not in ("", "success") if from_systemd
                  else bk.get("success") == 0)
        if failed:
            return [], "", [], 0
        skipped, free = _dump_skipped(bk)
        return _dump_problems(bk), _DUMP_REASON_BACKUP, skipped, free
    if bk.get("dumps"):
        now_ts = now.timestamp()
        broken = sorted(_dump_label(d) for d in bk["dumps"] if dump_local_stale(d, now_ts))
        skipped, free = _dump_skipped(bk)
        return broken, _DUMP_REASON_LOCAL, skipped, free
    return [], "", [], 0


def _server_conditions(s: Server, now: datetime,
                       web_err: tuple[float, float, int, dict, float] | None = None,
                       pod_names: dict[str, str] | None = None) -> dict[str, tuple[int, dict]]:
    """Пороги сервера: ключ → (уровень, контекст для шаблона текста). Уровень 0 =
    норма; для диска 1=предупреждение(≥warn), 2=проблема(≥alert), 3=критично(≥crit)."""
    out: dict[str, tuple[int, dict]] = {}
    state = s.alert_state or {}
    seen = s.last_seen is not None
    online = seen and (now - _aware(s.last_seen)).total_seconds() <= max(
        s.offline_after_seconds, 30
    )
    if seen and not panel_just_started(now):
        # Оффлайн алертим только если сервер был на связи и замолчал. Первые минуты
        # после подъёма панели пропускаем: там «молчат» все, и это наша пауза, не их.
        #
        # ДЕБАУНС. Порог offline_after (120с) отвечает на вопрос «показывать ли ноду
        # серой в интерфейсе» — для алерта он слишком чуткий: нода с занятым каналом
        # (докер тянет образ, идёт бэкап) промахивается мимо пары отчётов и уходит в
        # ленту, через полминуты возвращается, и так по кругу. Живьём наблюдалось
        # 8 сообщений за 30 минут по одной ноде. Для АЛЕРТА молчание должно продержаться
        # ещё _OFFLINE_ALERT_EXTRA сверх порога.
        silent = (now - _aware(s.last_seen)).total_seconds()
        alert_off = silent > max(s.offline_after_seconds, 30) + _OFFLINE_ALERT_EXTRA
        out["offline"] = (0 if online else (1 if alert_off else int(state.get("offline", 0))), {})

    # 0 = слать сразу (валидное значение!) → fallback на дефолт ТОЛЬКО при None
    _ss = getattr(s, "alert_sustain_seconds", None)
    sustain_s = _SUSTAIN_DEFAULT if _ss is None else max(int(_ss), 0)

    def sustain(key: str, breach: bool, ctx: dict, below: bool | None = None,
                cool_s: float | None = None, hold_s: float | None = None) -> None:
        """Пороговую метрику алертим лишь если превышение ДЕРЖИТСЯ ≥ sustain_s секунд
        (гасим кратковременные спайки). Момент начала «жарки» пишем в ctx["since"]
        (persist каждый тик); None = вернулось в норму / оффлайн → сброс.
        below - значение уже ниже линии снятия (порог минус _HYST). Пока алерт висит, а до
        линии не дошло, это все еще превышение; снимаем, когда ниже нее пробыло выдержку
        (ctx["cool"] - с какого момента). cool_s и hold_s - своя выдержка отбоя и срабатывания
        вместо общей."""
        alerted = below is not None and int(state.get(key, 0) or 0) >= 1
        cool = None
        if online and (breach or (alerted and not below)):
            since = state.get(f"{key}_since") or now.isoformat()
            started = _parse_iso(since)
            held = (now - started).total_seconds() if started else 0.0
            lvl = 1 if held >= (sustain_s if hold_s is None else hold_s) else 0
        elif online and alerted:
            started = _parse_iso(str(state.get(f"{key}_cool") or "")) or now
            if (now - started).total_seconds() < (sustain_s if cool_s is None else cool_s):
                since, lvl, cool = state.get(f"{key}_since"), 1, started.isoformat()
            else:
                since, lvl = None, 0
        else:
            since, lvl = None, 0
        ctx["since"] = since
        if below is not None:
            ctx["cool"] = cool
        out[key] = (lvl, ctx)

    def sustain_lvl(key: str, lvl: int, ctx: dict, cool_s: float | None = None) -> None:
        """Как sustain, но с уровнем: худшая причина сейчас, если беда держится дольше выдержки.
        cool_s - отбой ушедшего алерта только после стольких секунд спокойствия: заходы внутри
        этого времени - одна история, уровень держится прежний."""
        alerted = int(state.get(key, 0) or 0) if cool_s is not None else 0
        cool = None
        if lvl and online:
            since = state.get(f"{key}_since") or now.isoformat()
            started = _parse_iso(since)
            held = (now - started).total_seconds() if started else 0.0
            lvl = lvl if held >= sustain_s else 0
            if alerted and not lvl:
                lvl = alerted  # снова пошло внутри истории: уровень тот же, без нового сообщения
        elif online and alerted:
            started = _parse_iso(str(state.get(f"{key}_cool") or "")) or now
            if (now - started).total_seconds() < cool_s:
                since, lvl, cool = state.get(f"{key}_since"), alerted, started.isoformat()
            else:
                since, lvl = None, 0
        else:
            since, lvl = None, 0
        ctx["since"] = since
        if cool_s is not None:
            ctx["cool"] = cool
        out[key] = (lvl, ctx)

    rep = s.last_report or {}
    cpu = rep.get("cpu_percent")
    if s.cpu_alert_percent and cpu is not None:
        # cause заполняется в момент отправки (нужна история метрик), но в ctx он
        # обязан быть всегда: без него шаблон с {cause} не отрендерится
        sustain("cpu", cpu >= s.cpu_alert_percent,
                {"value": round(cpu), "threshold": s.cpu_alert_percent, "cause": ""},
                below=cpu < s.cpu_alert_percent - _HYST)
    if s.mem_alert_percent and rep.get("mem_total"):
        memp = rep.get("mem_used", 0) / rep["mem_total"] * 100
        sustain("mem", memp >= s.mem_alert_percent,
                {"value": round(memp), "threshold": s.mem_alert_percent, "cause": ""},
                below=memp < s.mem_alert_percent - _HYST)
    if rep.get("disks"):
        worst, worst_mount = max(
            ((d["used"] / d["total"] * 100, d.get("mount")) for d in rep["disks"] if d.get("total")),
            default=(0.0, None), key=lambda x: x[0],
        )
        crit, prob, warn = s.disk_crit_percent, s.disk_alert_percent, s.disk_warn_percent
        if crit and worst >= crit:
            lvl, sev, thr = 3, "критично", crit
        elif prob and worst >= prob:
            lvl, sev, thr = 2, "проблема", prob
        elif warn and worst >= warn:
            lvl, sev, thr = 1, "предупреждение", warn
        else:
            lvl, sev, thr = 0, "", 0
        out["disk"] = (lvl, {"value": round(worst), "threshold": thr,
                             "severity": sev, "level": lvl, "cause": "", "mount": worst_mount})
        # Inode кончаются отдельно от места: миллионы мелких файлов (сессии PHP, кэш) забивают
        # ФС, когда df показывает свободные гигабайты, и ничего нового не создать. Пороги те
        # же, что у места. Считают агенты 2.15+, у btrfs inode нет вовсе.
        ino = [d for d in rep["disks"] if d.get("inodes")]
        if ino:
            wi = max(ino, key=lambda d: (d.get("inodes_used") or 0) / d["inodes"])
            ipct = (wi.get("inodes_used") or 0) / wi["inodes"] * 100
            if crit and ipct >= crit:
                ilvl, ithr = 3, crit
            elif prob and ipct >= prob:
                ilvl, ithr = 2, prob
            elif warn and ipct >= warn:
                ilvl, ithr = 1, warn
            else:
                ilvl, ithr = 0, 0
            out["inode"] = (ilvl, {"value": round(ipct), "threshold": ithr, "level": ilvl,
                                   "mount": wi.get("mount") or "?", "cause": ""})
    temp = rep.get("cpu_temp")
    if s.temp_alert_c and temp is not None:
        sustain("temp", temp >= s.temp_alert_c,
                {"value": round(temp), "threshold": s.temp_alert_c})
    thr = rep.get("cpu_throttle")
    if thr is not None:  # только если сервер УМЕЕТ мерить троттлинг (на VM обычно нет)
        # Троттлинг алертим ТОЛЬКО когда он реально требует вмешательства:
        # процессор горячий И тормозит НЕСКОЛЬКО интервалов подряд (недоохлаждение).
        # Счётчик ядра дёргается и при 49-77°C на 4-10% CPU (наблюдалось) — это
        # микро-спайки на доли секунды, само-восстановление за тик; такое — шум,
        # не алерт. Гейт по t° отсекает «холодный» троттлинг, стрик — одиночные пики.
        tnow = rep.get("cpu_temp")
        real = thr > 0 and (tnow is None or tnow >= _THROTTLE_TEMP_FLOOR)
        prev_streak = int((s.alert_state or {}).get("throttle_streak", 0))
        streak = prev_streak + 1 if real else 0
        lvl = 1 if streak >= _THROTTLE_MIN_STREAK else 0
        out["throttle"] = (lvl, {"value": round(thr), "streak": streak})
    ctmax = rep.get("conntrack_max") or 0
    if s.conntrack_alert_percent and ctmax > 0:  # только если conntrack есть
        fill = (rep.get("conntrack_count") or 0) / ctmax * 100
        sustain("conntrack", fill >= s.conntrack_alert_percent,
                {"value": round(fill), "threshold": s.conntrack_alert_percent})

    # Коннекты СУБД. Слоты кончаются задолго до того, как что-то заметно по CPU или
    # памяти самой базы: она жива, отвечает, метрики зелёные — а приложение уже
    # получает «sorry, too many clients already». Берём САМЫЙ нагруженный движок ноды:
    # алерт на сервер один, а инстансов на нём бывает несколько, и молчать из-за того,
    # что второй свободен, нельзя. Дебаунс общий (sustain): всплеск коннектов на один
    # интервал — обычное дело у пулеров, инцидент — только удержание.
    if s.db_conn_alert_percent:
        worst = db_conn_worst(rep)
        if worst is not None:
            pct, db = worst
            name = db.get("container") or db.get("engine") or "СУБД"
            sustain("db_conn", pct >= s.db_conn_alert_percent, {
                "value": round(pct),
                "threshold": s.db_conn_alert_percent,
                "engine": name,
                "used": db.get("conn_used") or 0,
                "limit": db.get("conn_max") or 0,
            })
    # Сроки Kubernetes и Flux. Отдельный класс отказов: он не проявляется ни ростом
    # метрик, ни падением подов. Истёкший токен Flux просто перестаёт привозить новое —
    # запущенное продолжает работать, дашборды остаются зелёными, и пропажу замечают
    # через дни, по недоехавшей выкатке. Сертификаты control-plane отказывают резче, но
    # так же без предупреждения. Берём САМЫЙ ранний срок: алерт на сервер один, а
    # датируемых сущностей на ноде десятки.
    warn_days = kube_warn_days(s)
    if warn_days:
        soon = kube_expiry_soon(rep, warn_days, now)
        if soon is not None:
            days = soon[1]
            sustain("kube_expiry", True, kube_expiry_ctx(*soon))
            # Уровень - сколько порогов пройдено, и истекший сверх них: за 7 дней, за
            # 1 день и в день истечения - по сообщению. Раньше было одно, за две недели,
            # и к сроку о нем успевали забыть.
            lvl, ctx = out["kube_expiry"]
            if lvl:
                ctx["level"] = sum(1 for d in warn_days if days <= d) + (1 if days < 0 else 0)
                out["kube_expiry"] = (ctx["level"], ctx)
        else:
            sustain("kube_expiry", False, {})

    # Flux уже сломан. Второй половиной той же беды: срок можно проспать, а токен —
    # ещё и отозвать руками, и тогда предупреждать не о чем, доставка встала сразу.
    # Читаем Ready у ресурсов Flux; «в процессе» не считаем поломкой, а дебаунс
    # (те же 15 минут по умолчанию) гасит обычную реконсиляцию.
    if rep.get("flux") is not None:
        fx = flux_state(rep)
        if fx is not None and fx[0] == "flux_stale":
            sustain("flux_stale", True, fx[1], hold_s=_FLUX_STALE_HOLD)
            sustain("flux_down", False, {})
        elif fx is not None:
            sustain("flux_down", True, fx[1])
            sustain("flux_stale", False, {})
        else:
            sustain("flux_down", False, {})
            sustain("flux_stale", False, {})

    # Пошли ошибки 5xx. Выдержка тут - само окно в 15 минут, поэтому уровень ставим сразу,
    # без sustain: иначе к окну добавились бы ещё 15 минут ожидания.
    w5 = web_5xx_now(s, web_err, now) if online else None
    if w5 is not None:
        bad5, errs, total, thr5 = w5
        # Отбой - только после часа подряд без превышения (_WEB_ERR_CLEAR): пачки ошибок
        # внутри часа остаются одной историей. «since» здесь - с какого момента чисто.
        lvl5, since5 = (1, None) if bad5 else (0, None)
        if not bad5 and int(state.get("web_5xx", 0)) > 0:
            since5 = state.get("web_5xx_since") or now.isoformat()
            clean_from = _parse_iso(since5)
            if clean_from is None or now - clean_from < _WEB_ERR_CLEAR:
                lvl5 = 1
        out["web_5xx"] = (lvl5, {
            "value": f"{errs / total * 100:.2f}" if total else "0",
            "errs": int(errs), "total": int(total),
            "where": "",  # заполняется при отправке: нужна история минут с ошибками
            "threshold": thr5, "since": since5,
        })

    # Поды, которые контроллер обязан держать живыми. Повод - k8s-b 23.09.2026:
    # ClickHouse (нет секрета) и PostgreSQL (нет сертификата) лежали по семь часов, и
    # панель об этом не сказала ни разу: в «Кубере» их видно, только если открыть.
    # Шума не будет: по парку в 26 нод таких подов было ровно четыре, все на этой ноде.
    if online and (rep.get("kube") or {}).get("access"):
        bad_pods = kube_bad_pods(rep)
        sustain("kube_pod", bool(bad_pods),
                {"pods": bad_pods_text(bad_pods), "n": len(bad_pods)},
                below=not bad_pods, cool_s=_POD_CALM_S)

    if s.disk_temp_alert_c:  # макс по устройствам с датчиком (на VM датчика обычно нет)
        temps = [d["temp"] for d in (rep.get("disk_devs") or []) if d.get("temp") is not None]
        if temps:
            hottest = max(temps)
            sustain("disktemp", hottest >= s.disk_temp_alert_c,
                    {"value": round(hottest), "threshold": s.disk_temp_alert_c})

    # Поломка физического диска (helper diskhealth-setup). Повод - k8s-d 05.10.2026:
    # NVMe умер, оба RAID1 работали на одном диске, а панель молчала. Нет свежего блока -
    # ключ не отдаем вовсе: молчание helper'а не повод объявлять, что RAID починился, а у
    # недоступной ноды свой алерт. Выдержки нет: развалившийся массив сам не соберется.
    hb = disk_health_block(rep) if online else None
    if hb is not None:
        probs = disk_health_problems(hb, wear_hist=s.disk_wear, writers=rep.get("disk_writers"))
        # Ошибки ввода-вывода: окно helper'а 10 минут, сверху держим час с последней пачки
        # (since), чтобы пачки внутри часа были одной историей. Момент пачки обновляем раз
        # в 5 минут: иначе alert_state переписывался бы каждый тик, пока сыплются ошибки.
        io = hb.get("io") if isinstance(hb.get("io"), dict) else {}
        io_n = _int(io.get("count"))
        prev_io = state.get("disk_health_since")
        last_io = _parse_iso(prev_io or "")
        io_since = None
        if io_n >= _DISK_IO_MIN:
            fresh = last_io is not None and now - last_io < timedelta(minutes=5)
            io_since = prev_io if fresh else now.isoformat()
        elif last_io is not None and now - last_io < _DISK_IO_HOLD:
            io_since = prev_io
        if io_since:
            txt = (f"ошибки ввода-вывода в журнале ядра: {io_n} за 10 минут" if io_n >= _DISK_IO_MIN
                   else "ошибки ввода-вывода в журнале ядра, последние меньше часа назад")
            line = next((str(x) for x in reversed(io.get("last") or []) if x), "")
            if line:
                txt += f" ({line[:160]})"
            probs.append((2, "", txt))
            probs.sort(key=lambda p: -p[0])
        lvl = max((p[0] for p in probs), default=0)
        out["disk_health"] = (lvl, {
            "detail": disk_health_text(probs), "level": lvl,
            # что именно сломано: новая поломка при том же уровне - повод для сообщения
            "sig": sorted({k for _l, k, _t in probs if k}),
            "since": io_since,
        })

    # Прогноз заполнения (планировщик, раз в полчаса по истории): место или inode кончатся
    # через пять суток - предупреждение, через сутки - проблема. Обычные пороги говорят,
    # насколько полно сейчас, прогноз - сколько осталось. Гистерезис по часам: прогноз
    # дышит вместе с ростом, и без запаса алерт мигал бы на границе. Протухший прогноз
    # ключа не дает: молчание - не повод объявлять, что рост прекратился.
    fitems = dfc.fresh_items(s.disk_forecast, now) if online else None
    if fitems is not None:
        prev_fc = int(state.get("disk_forecast", 0))
        eta = float(fitems[0]["eta_h"]) if fitems else None
        if eta is not None and (eta <= _FORECAST_CRIT_H
                                or (prev_fc >= 2 and eta <= _FORECAST_CRIT_H * 1.25)):
            f_lvl = 2
        elif eta is not None and (eta <= _FORECAST_WARN_H
                                  or (prev_fc and eta <= _FORECAST_WARN_H * 4 / 3)):
            f_lvl = 1
        else:
            f_lvl = 0
        out["disk_forecast"] = (f_lvl, {
            "detail": dfc.forecast_text(fitems, _FORECAST_WARN_H) if f_lvl else "",
            "level": f_lvl,
        })

    # Упавшие юниты systemd (helper units-setup). certbot, упавший на продлении, молчит до
    # истечения сертификата, бэкап по таймеру - до дня, когда понадобится восстановление.
    # Юниты, которые человек велел не замечать, в счет не идут. Нет свежего блока - ключа
    # нет: молчание helper'а не повод объявлять, что все починилось.
    ub = units_block(rep) if online else None
    if ub is not None:
        ref = float(rep.get("clock_unix") or 0) or now.timestamp()
        muted_u = units_muted(s, now)
        covered = backup_units(s, rep)
        bad = [u for u in ub.get("units") or []
               if isinstance(u, dict) and u.get("unit") and u["unit"] not in muted_u
               and not own_helper_unit(str(u["unit"])) and not benign_unit(str(u["unit"])) and u["unit"] not in covered
               and ref - float(u.get("since") or 0) >= _UNIT_MIN_AGE]
        hold = units_hold(s, bad, now, muted_u)
        u_lvl = max((h[1] for h in hold.values()), default=0)
        out["units"] = (u_lvl, {
            "detail": units_text(bad, ref) if bad else "", "level": u_lvl,
            # какие юниты лежат: новый упавший при том же уровне - повод для сообщения
            "sig": sorted(hold), "hold": hold,
        })

    # Одинаковые процессы, которые давно крутят по ядру каждый (агент 2.21). На k8s-a-prc 47
    # зависших chrome жгли 45 ядер из 96, а CPU-алерт молчал: до порога не доходило. Агент
    # старше 2.21 групп не шлет - None, как у юнитов без helper'а.
    if online and rep.get("cpu_groups") is not None:
        spin = spin_groups(rep)
        out["cpu_spin"] = (1 if spin else 0, {"detail": spin_text(spin, pod_names) if spin else ""})

    # Прогон бэкапа добавил в разы больше обычного (helper backup-setup 0.32). Без блока helper'а
    # ключа нет: молчание не значит, что прирост стал обычным.
    if online and backup_growth.EXTRA_KEY in (rep.get("extras") or {}):
        g = backup_growth.jump(s, now)
        out["backup_growth"] = (1 if g else 0, {"detail": g["text"] if g else ""})

    # Внешний прокси (caddy-docker-proxy, traefik) с docker-сокетом: взлом прокси - root на
    # хосте. Только при доступе к докеру: без списка контейнеров молчание ничего не значит.
    # sig - какие контейнеры: новый такой же прокси на ноде - повод для нового сообщения.
    if online and (rep.get("docker") or {}).get("access"):
        ex = docker_exposure.exposed(rep)
        out["docker_sock"] = (1 if ex else 0, {"detail": docker_exposure.text(ex),
                                               "sig": sorted(e["name"] for e in ex)})
        # Контейнер с ключами от хоста сверх положенного по назначению (privileged, опасные
        # capabilities, docker-сокет, корень хоста на запись). Пункт - в "Требует действий",
        # заглушенные точечно ("dockrisk:<имя>") не в счет; sig - контейнер и его ключи.
        dr = docker_risk.advice(s)
        out["docker_risk"] = (1 if dr else 0, {"detail": docker_risk.text(dr),
                                               "sig": sorted(f"{x['name']}:{','.join(x['flags'])}" for x in dr)})

    # Сдвиг часов: локальное время ноды (clock_unix) vs время панели на приёме. По модулю;
    # порог warn 5с / проблема 30с / крит 5мин. Дебаунс (как sustain): разовый спайк —
    # медленный отчёт/GC — не шлёт алерт, ждём удержания ≥ sustain_s (пишем clock_since).
    skew = rep.get("clock_skew_sec")
    if online and isinstance(skew, (int, float)):
        a = abs(int(skew))
        raw = 3 if a >= 300 else 2 if a >= 30 else 1 if a >= 5 else 0
        if raw > 0:
            c_since = state.get("clock_since") or now.isoformat()
            c_started = _parse_iso(c_since)
            c_held = (now - c_started).total_seconds() if c_started else 0.0
            c_lvl = raw if c_held >= sustain_s else 0
        else:
            c_since, c_lvl = None, 0
        disp = f"{a} с" if a < 120 else f"{a // 60} мин"
        out["clock"] = (c_lvl, {"value": disp, "since": c_since})

    # DNS ноды (агент 2.25): медленно или не резолвит дольше _DNS_HOLD - проблема. Без снимка
    # (агент старше или замер встал) ключа нет: молчание не значит, что DNS в порядке.
    if online and dns_block(rep) is not None:
        why = dns_problem(rep)
        if why:
            d_since = state.get("dns_since") or now.isoformat()
            d_started = _parse_iso(d_since)
            d_held = (now - d_started).total_seconds() if d_started else 0.0
            d_lvl = 2 if d_held >= _DNS_HOLD else 0
        else:
            d_since, d_lvl = None, 0
        # С какого момента подряд тормозит или молчит каждый резолвер (alert_state dns_res): в
        # "Что сломано" попадает тот, что лежит дольше выдержки, пока соседние отвечают
        prev_res = state.get("dns_res") if isinstance(state.get("dns_res"), dict) else {}
        res = {x["addr"]: prev_res.get(x["addr"]) or now.isoformat()
               for x in dns_slow_servers(dns_block(rep) or {})}
        out["dns"] = (d_lvl, {"details": why, "since": d_since, "res": res})

    # Бэкап (restic): алертим только по РЕАЛЬНОЙ метрике и только пока сервер онлайн
    # (у оффлайна свои алерты; данные протухли). Это устойчивые состояния (не спайки),
    # поэтому уровень выставляем напрямую, без дебаунса.
    bk = rep.get("backup") or {}
    # «Бэкап не настроен»: по умолчанию у каждого сервера должен быть бэкап. Не алертим
    # на бэкап-серверах (себя не бэкапят). Галочка «не требуется» → level 0 (даём
    # восстановиться, а не убираем условие — иначе прошлый алерт не закроется).
    if online and (rep.get("backup_server") or {}).get("present"):
        # Бэкап-сервер себя не бэкапит → условие снято. Ключ отдаём ЯВНО (0), а не молчим:
        # цикл разбора алертов ходит только по присутствующим условиям, поэтому пропуск
        # ключа оставлял бы ранее сработавший алерт висеть вечно (нода стала бэкап-сервером
        # — алерт «бэкап не настроен» уже не закрывался никогда).
        out["backup_missing"] = (0, {})
    elif online:
        # свой файловый бэкап ноды (restic/borg/rsync, настроенный без панели) — тоже бэкап
        ok = (bool(bk.get("configured")) or bool(getattr(s, "backup_not_required", False))
              or custom_backups.has_file_backup(s, now))
        if ok:
            out["backup_missing"] = (0, {})
        else:
            # Отсрочка суток. Алерт НЕ срочный, а без неё он бил дважды впустую:
            # (1) сразу после заведения новой ноды — до того, как её вообще успели
            # настроить; (2) в окно переконфигурации бэкапа на уже живой ноде.
            # Считаем И длительность состояния, И возраст ноды: молчим, пока мал любой.
            since = state.get("backup_missing_since") or now.isoformat()
            started = _parse_iso(since)
            held = (now - started).total_seconds() if started else 0.0
            created = getattr(s, "created_at", None)
            # нет created_at (старые записи) → возрастом не ограничиваем, хватит held
            age = (now - _aware(created)).total_seconds() if created else _BACKUP_MISSING_GRACE
            # УЖЕ висящий алерт отсрочкой не гасим: иначе на переходе к этой логике ушло бы
            # ложное «восстановлено», хотя бэкапа как не было, так и нет. Отсрочка гейтит
            # только ПЕРВОЕ срабатывание.
            already = int(state.get("backup_missing", 0) or 0) >= 1
            lvl = 1 if already or (held >= _BACKUP_MISSING_GRACE and age >= _BACKUP_MISSING_GRACE) else 0
            out["backup_missing"] = (lvl, {"since": since})
    # Метрика restic — основной источник. Ноды со старой ансибл-раскладкой её не пишут:
    # там агент (1.49+) берёт время прогона из systemd (ts_source=systemd), а успех — из
    # Result юнита. Иначе полностью рабочий бэкап был бы для алертов невидим: протух —
    # никто не узнает.
    from_systemd = bk.get("ts_source") == "systemd"
    if online and (bk.get("metric_present") or from_systemd):
        if from_systemd:
            res = (bk.get("service_result") or "").strip()
            # пустой Result бывает пока сервис ни разу не отработал — это не «упал»
            out["backup_failed"] = (1 if res not in ("", "success") else 0, {})
        else:
            out["backup_failed"] = (1 if bk.get("success") == 0 else 0, {})
        last_ts = bk.get("last_backup_ts") or 0
        if last_ts:
            age = now.timestamp() - last_ts
            out["backup_stale"] = (1 if age > _BACKUP_STALE_SECONDS else 0,
                                   {"days": round(age / 86400, 1)})
        # Дампы СУБД проверяем ТОЛЬКО когда файловый бэкап успешен: если он сам упал,
        # дамп мог и не запуститься — это уже backup_failed, дублировать не нужно.
        failed_lvl, _ = out.get("backup_failed", (0, {}))
        if failed_lvl == 0:
            broken, reason, skipped, free = dump_issues(rep, now)
            # через sustain: сломанный дамп — состояние на дни, оно удержание переживёт,
            # а одиночный тик на стыке циклов (см. _BACKUP_DUMP_LAG_SECONDS) — нет
            sustain("backup_dump", bool(broken),
                    {"engines": ", ".join(broken), "n": len(broken), "reason": reason})
            out["backup_dump_space"] = (1 if skipped else 0,
                                        {"engines": ", ".join(skipped), "free": free})
    elif online and (bk.get("dumps") or state.get("backup_dump") or state.get("backup_dump_space")):
        # Файлового бэкапа нет, а дампы есть: helper снимает их своим таймером, и сверять
        # их не с чем, кроме часов (см. dump_local_stale). Висящий алерт при выключенных
        # дампах тоже сюда: условие должно прийти с нулём, иначе он не закроется.
        broken, _reason, skipped, free = dump_issues(rep, now)
        sustain("backup_dump", bool(broken),
                {"engines": ", ".join(broken), "n": len(broken), "reason": _DUMP_REASON_LOCAL})
        out["backup_dump_space"] = (1 if skipped else 0,
                                    {"engines": ", ".join(skipped), "free": free})
    # Дамп-CronJob'ы в кластере: мониторим прогоны НЕЗАВИСИМО от restic-бэкапа (kube-нода
    # часто без него). Панель раньше только детектила «дамп настроен» — теперь алертит,
    # если CronJob приостановлен или его последний прогон не завершился успехом.
    if online and (rep.get("kube") or {}).get("access"):
        cron = _cron_dump_problems(rep, now)
        out["backup_cron"] = (1 if cron else 0, {"jobs": "; ".join(cron[:6]), "n": len(cron)})
        # Остальные CronJob кластера: задание по расписанию упало или перестало отрабатывать.
        # Предупреждение, как у упавшего oneshot-юнита: сервис живой, не сделана работа.
        kc = sorted(f"{n} ({why})" for n, why in cron_job_issues(rep, now, dumps=False).items())
        out["kube_cron"] = (1 if kc else 0, {"jobs": "; ".join(kc[:6]) + (f" и еще {len(kc) - 6}" if len(kc) > 6 else ""),
                                             "n": len(kc)})
    # Свои бэкапы ноды (настроены без панели; находит helper): упал прогон, давно не было
    # успешного, выключен таймер. Состояния устойчивые, как и у restic, — без дебаунса.
    if online:
        own = custom_backups.problems(s, now)
        out["backup_custom"] = (1 if own else 0, {"jobs": "; ".join(own[:6]), "n": len(own)})
    # Опасный (2) или служебный (1) порт открыт в интернет (внешняя проверка портов): состояние,
    # пока не закроют файрволом или не отметят порт намеренным. Свежесть - в open_ports.exposed
    if online:
        ex = open_ports.exposed(s, now)
        out["exposed"] = (open_ports.exposed_level(ex), {"detail": open_ports.exposure_text(ex), "n": len(ex),
                                                         "own": open_ports.from_panel(s)})
    # Сервер задыхается (агент 2.26): уровень - худшая из причин, выдержка - как у CPU и RAM
    if online and any(rep.get(k) for k in ("psi", "dstate", "fds")):
        pr = pressure_problems(rep)
        sustain_lvl("pressure", pr[0][0] if pr else 0, cool_s=_CALM_S, ctx={"detail": "; ".join(t for _l, _r, t in pr[:3]),
                                                        "res": pr[0][1] if pr else "", "cause": "",
                                                        "hint": pressure_hint(rep)})
    # Сеть ноды: потери TCP, очередь accept, conntrack, ошибки и дропы интерфейсов
    if online and (rep.get("tcp") or rep.get("net_ifaces")):
        nets = net_problems(rep)
        hint = net_hint(rep, nets)
        sustain_lvl("net_health", nets[0][0] if nets else 0,
                    {"detail": "; ".join(t for _l, _r, t in nets[:4]) + (f"\n↳ {hint}" if hint else "")},
                    cool_s=_CALM_S)
    # Связь с другими серверами (агент 2.31, app/netlinks.py). sig - какие связи в беде: новая
    # сломанная связь при уже объявленной - новое сообщение, та же снова - нет (держим до отбоя)
    links_state = getattr(s, "links_state", None)
    if online and (links_state or state.get("link")):
        lk = netlinks.problems(links_state)
        sig = {str(i) for _l, i, _t in lk}
        if int(state.get("link", 0) or 0):
            sig |= set(state.get("link_sig") or [])
        sustain_lvl("link", lk[0][0] if lk else 0, {"detail": "; ".join(t for _l, _i, t in lk[:4]),
                                                    "sig": sorted(sig)}, cool_s=_CALM_S)
    # Трафик против лимита тарифа (app/traffic.py): прогноз ходит у границы, поэтому отбой только
    # после полусуток спокойствия
    tl, tt = traffic_mod.problem(getattr(s, "traffic_sum", None), now)
    if online and (tl or state.get("traffic")):
        sustain_lvl("traffic", tl, {"detail": tt}, cool_s=_TRAFFIC_CALM_S)
    # Часто перезапускается: приросты счетчиков за сутки ведет цикл алертов (alert_state restarts).
    # sig - что именно, чтобы новый контейнер в той же беде дал свой алерт
    if online and isinstance((s.alert_state or {}).get("restarts"), dict):
        rp = srv_restarts.problems(s, now)
        for kind, key in (("docker", "docker_restarts"), ("pod", "kube_restarts")):
            items = [x for x in rp if x["kind"] == kind]
            out[key] = (1 if items else 0, {"detail": "; ".join(x["text"] for x in items[:4]),
                                            "sig": [x["key"] for x in items]})
    # Пропал порт, который слушался неделями (история - alert_state listen_hist, ведет цикл алертов)
    if online and isinstance((s.alert_state or {}).get("listen_hist"), dict):
        lg = listen_watch.problems(s, now)
        out["listen_gone"] = (1 if lg else 0, {"detail": "; ".join(x["text"] for x in lg[:3]),
                                               "sig": [x["key"] for x in lg]})
    # Диск тормозит: задержка выше порога держится дольше выдержки
    if online and rep.get("disk_devs"):
        dl, dtext = disk_slow(rep)
        sustain_lvl("disk_slow", dl, {"detail": dtext}, cool_s=_CALM_S)
    # GPU (helper gpu-setup): драйвер, пропавшая карта, Xid - без выдержки, как у дисков: сами не
    # починятся; sig - что именно, новая беда при том же уровне дает свой алерт. Перегрев - с выдержкой
    if online and gpu_mod.block(rep) is not None:
        gp = gpu_mod.problems(rep, now)
        out["gpu"] = (max((p[0] for p in gp), default=0),
                      {"detail": "; ".join(p[1] for p in gp[:3]), "sig": [p[2] for p in gp]})
        hl, htext = gpu_mod.heat(rep, now)
        sustain_lvl("gpu_heat", hl, {"detail": htext}, cool_s=_CALM_S)
    # LLM-сервисы (агент 2.36): молчание /metrics, очередь при полном KV-кэше, задержки в разы выше
    # обычного - когда держится дольше выдержки
    if online and gpu_mod.llm_list(rep):
        lp = gpu_mod.llm_problems(rep, state.get("llm_base"))
        sustain_lvl("llm", max((p[0] for p in lp), default=0), {"detail": "; ".join(p[1] for p in lp[:3])},
                    cool_s=_CALM_S)
    # Сертификат nginx истекает или истек (helper webserver-setup 0.24)
    if online and web_certs.block(rep, now) is not None:
        wce = web_certs.problems(rep, now)
        out["web_cert"] = (max((lv for lv, _t, _k in wce), default=0),
                           {"detail": "; ".join(t for _l, t, _k in wce[:3]), "sig": [k for _l, _t, k in wce]})
    # Конфиг веб-сервера не проходит проверку (helper webserver-setup 0.23): сайты упадут при
    # следующем reload. sig - какой именно, чтобы второй сломанный конфиг дал свой алерт
    if online and web_config.block(rep, now) is not None:
        wc = web_config.problems(rep, now)
        out["web_config"] = (max((lv for lv, _t, _k in wc), default=0),
                             {"detail": "; ".join(t for _l, t, _k in wc[:3]), "sig": [k for _l, _t, k in wc]})
    # Здоровье баз (helper dbstat-setup 0.5): реплика, долгие транзакции, блокировки, wraparound,
    # VACUUM. sig - что именно, чтобы новая беда при том же уровне дала свой алерт
    if online and db_health.block(rep, now) is not None:
        dh = db_health_problems(s, rep, now)
        out["db_health"] = (max((lv for lv, _t, _k in dh), default=0),
                            {"detail": "; ".join(t for _l, t, _k in dh[:3]), "sig": [k for _l, _t, k in dh]})
    # SSH (helper changes-setup 0.3): root по паролю на открытом порту, перебор без защиты
    if online and ssh_audit.block(rep) is not None:
        sp = ssh_audit.problems(s, now)
        out["ssh_risk"] = (max((lv for lv, _t in sp), default=0), {"detail": "; ".join(t for _l, t in sp)})
    # Второй root (uid 0) или учетка с пустым паролем: так выглядит закладка или забытая дыра
    if online and srv_changes.snapshot(rep) is not None:
        ar = access_audit.problems(s)
        out["access_risk"] = (max((lv for lv, _t in ar), default=0),
                              {"detail": "; ".join(t for _l, t in ar), "sig": sorted(t for _l, t in ar)})
    # Очередь растет и не разбирается: полчаса роста уже в самом правиле, выдержка не нужна.
    # sig - какие очереди, вторая застрявшая дает свой алерт
    if online and isinstance((s.alert_state or {}).get("qwatch"), dict):
        qs = queue_watch.problems(s, now)
        out["queue_stuck"] = (max((x["level"] for x in qs), default=0),
                              {"detail": "; ".join(x["text"] for x in qs[:3])
                                         + (f"; и еще {len(qs) - 3}" if len(qs) > 3 else ""),
                               "sig": [x["key"] for x in qs]})
    # Всплеск упавших задач: окно в час уже в правиле, выдержка не нужна. sig - какие брокеры
    if online and queue_watch.helper_brokers(rep):
        fp = queue_watch.failed_problems(s, now)
        out["queue_failed"] = (max((x["level"] for x in fp), default=0),
                               {"detail": "\n".join(x["text"] for x in fp[:3])
                                          + (f"\nи еще {len(fp) - 3}" if len(fp) > 3 else ""),
                                "sig": [x["key"] for x in fp]})
    # Воркеры под supervisor и pm2: выдержка уже в правилах (BACKOFF три минуты, три перезапуска
    # за 15 минут), FATAL - сразу. sig - какие воркеры, второй упавший дает свой алерт
    if online and workers_mod.block(rep) is not None:
        wp = workers_mod.problems(s, now)
        out["workers"] = (max((x["level"] for x in wp), default=0),
                          {"detail": "\n".join(x["text"] for x in wp[:3])
                                     + (f"\nи еще {len(wp) - 3}" if len(wp) > 3 else ""),
                           "sig": [x["key"] for x in wp]})
    # Кроны: выдержка уже в правилах (вторая копия идет 15 минут, висит - не раньше часа). sig -
    # какие задачи: вторая наслоившаяся дает свой алерт
    if online and cron_watch.block(rep) is not None:
        cp = cron_watch.problems(s, now)
        out["cron_stuck"] = (max((x["level"] for x in cp), default=0),
                             {"detail": "\n".join(x["text"] for x in cp[:3])
                                        + (f"\nи еще {len(cp) - 3}" if len(cp) > 3 else ""),
                              "sig": [x["key"] for x in cp]})
    # Всплеск ошибок в логах: окно в час уже в правиле, выдержка не нужна. sig - какие логи
    if online and log_errors.block(rep) is not None:
        lp = log_errors.problems(s, now)
        out["log_errors"] = (max((x["level"] for x in lp), default=0),
                             {"detail": "\n".join(x["text"] for x in lp[:3])
                                        + (f"\nи еще {len(lp) - 3}" if len(lp) > 3 else ""),
                              "sig": [x["key"] for x in lp]})
    # PHP-FPM: окно в час уже в правиле (минуты очереди, счет за час), выдержка не нужна. sig - какие пулы
    if online and fpm_pools.block(rep) is not None:
        fp = fpm_pools.problems(s, now)
        out["fpm"] = (max((x["level"] for x in fp), default=0),
                      {"detail": "\n".join(x["text"] for x in fp[:3])
                                 + (f"\nи еще {len(fp) - 3}" if len(fp) > 3 else ""),
                       "sig": [x["key"] for x in fp]})
    # Утечка памяти: дно растет суток подряд (история - в alert_state mem_trend, ведет цикл алертов)
    if online and isinstance((s.alert_state or {}).get("mem_trend"), dict):
        ml = [x for x in mem_trend.problems(s, now) if x["level"]]
        out["mem_leak"] = (max((x["level"] for x in ml), default=0),
                           {"detail": "; ".join(x["text"] for x in ml[:3]), "sig": [x["key"] for x in ml]})
    # Проверка восстановления: итог недельной проверки держится до следующей, выдержка не нужна
    if online and backup_verify.block(rep) is not None:
        bv = backup_verify.problems(rep, now)
        out["backup_restore"] = (bv[0][0] if bv else 0, {"detail": "; ".join(t for _l, t in bv[:3])})
    # Ноды кластера: NotReady или под давлением дольше выдержки (перезагрузка ноды - минуты)
    if online and (rep.get("kube") or {}).get("access"):
        kn = kube_node_problems(rep)
        sustain_lvl("kube_node", 2 if kn else 0,
                    {"detail": "; ".join(kn[:4]) + (f" и еще {len(kn) - 4}" if len(kn) > 4 else "")})
    return out


def _rule_scope_ok(rule: dict, s: Server) -> bool:
    st = rule.get("scope_type", "all")
    if st == "all":
        return True
    scope = rule.get("scope") or []
    if st == "groups":
        return (s.group_name or "") in scope
    if st == "servers":
        return s.id in scope
    return True


def _fmt_rule(template: str, s: Server, ctx: dict) -> str:
    try:
        return template.format(server=s.name, group=s.group_name or "", **ctx)
    except (KeyError, IndexError, ValueError):
        return template  # некорректный шаблон — шлём как есть


async def evaluate_servers(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> None:
    """Оффлайн-детект и пороговые алерты серверов (по одному алерту на условие,
    recovery при возврате в норму). Каналы/тихие часы — как у остальных алертов."""
    async with session_factory() as session:
        servers = list(
            await session.scalars(select(Server).where(Server.enabled.is_(True)))
        )
        cfg = await settings_store.get_alert_config(session, settings)
        muted = await settings_store.get_muted(session)
        rules = await settings_store.get_server_alert_rules(session)
        web_err = await web_error_window(session, now)
        windows = list(await session.scalars(select(MaintenanceWindow)))
        # лежащие мониторы - чтобы назвать их в сообщении про сервер, от которого они зависят
        down_checks = [SimpleNamespace(id=i, name=n, target=t, type=ty, probe_local=pl, probe_server_id=ps,
                                       parent_mode=pm, parent_server_id=pp)
                       for i, n, t, ty, pl, ps, pm, pp in (await session.execute(select(
                           Check.id, Check.name, Check.target, Check.type, Check.probe_local,
                           Check.probe_server_id, Check.parent_mode, Check.parent_server_id,
                       ).where(Check.enabled.is_(True), Check.last_status == "down"))).all()]
    can_send = alerts.alerts_enabled(cfg) and not muted
    # окна обслуживания: сервер в идущем окне молчит, как под снузом (app/maint.py)
    maint_srv, _mc = maint.active(windows, now, cfg.get("tz") or "Europe/Moscow")
    # Зависимости (app/parents.py): кто сейчас молчит, что из-за этого лежит и кто стоит за ним.
    # Сервер за молчащим (или обслуживаемым) родителем про "недоступен" не пишет, он строкой
    # в сообщении про родителя; так же названы лежащие мониторы родителя.
    pidx = parents_mod.Index(servers)
    silent = parents_mod.silent_ids(servers, now, seen_online, panel_just_started(now))
    kids_down: dict[int, list[str]] = {}
    for _c in down_checks:
        for _pid, _why in parents_mod.parents_of(_c, pidx):
            kids_down.setdefault(_pid, []).append(_c.name)
    kids_quiet: dict[int, list[str]] = {}
    for _s in sorted(servers, key=lambda x: x.name):
        if _s.parent_id and _s.id in silent:
            kids_quiet.setdefault(_s.parent_id, []).append(_s.name)

    def deps_tail(sid: int) -> str:
        parts = []
        if kids_quiet.get(sid):
            parts.append(f"↳ за ним молчат: {html.escape(parents_mod.list_text(kids_quiet[sid]))}")
        if kids_down.get(sid):
            parts.append(f"↳ лежат мониторы: {html.escape(parents_mod.list_text(sorted(kids_down[sid])))}")
        return "".join("\n" + x for x in parts)

    base = settings.panel_url.rstrip("/")
    threshold = int(cfg.get("flood_threshold", 6))

    def srv_url(s: Server, key: str = "") -> str:
        """Ссылка на деталь сервера. Для типовых алертов добавляем &sec=<раздел>,
        чтобы клик открывал СРАЗУ нужный дашборд (OOM/RAM → «Память», CPU → «CPU» и т.д.).
        Docker-алерты ведут на раздел «Докер» с открытой карточкой хоста и сразу в ее раздел
        (?docker=id&sec=host)."""
        if not (base and s.id is not None):
            return ""
        if key.startswith("docker"):
            return f"{base}/?docker={s.id}" + (f"&sec={_DOCKER_SEC[key]}" if key in _DOCKER_SEC else "")
        if key in _SVC_SEC:
            # ведём СРАЗУ в раздел карточки сервисов ноды: из алерта по очереди человек идёт
            # смотреть, что там накопилось, по сертификату - в сертификаты
            return f"{base}/?services={s.id}&sec={_SVC_SEC[key]}"
        if key in _KUBE_TAB:
            # Раздел "Кубер" с открытым кластером и сразу на нужной вкладке: сроки, Flux,
            # поды. В карточке сервера кластера нет, и ссылка вела мимо.
            return f"{base}/?kube={s.id}&ktab={_KUBE_TAB[key]}"
        if key in _BSRV_SEC:
            return f"{base}/?backupsrv={s.id}&sec={_BSRV_SEC[key]}"
        if key.startswith("backup"):
            return f"{base}/?backup={s.id}" + (f"&sec={_BACKUP_SEC[key]}" if key in _BACKUP_SEC else "")
        sec = _SRV_SECTION.get(key)
        return f"{base}/?server={s.id}" + (f"&sec={sec}" if sec else "")

    base_cache: dict[int, dict[str, float]] = {}
    # имена подов по uid - один раз за тик: воркер без доступа к kube-api пишет поды по uid
    pod_names = pod_uid_names(servers)
    # имена нод панели - для сверки с клиентами бэкап-серверов (один раз за тик)
    panel_names = panel_server_names(servers)
    # на какие резолверы падает подозрение у каждой ноды с поломкой DNS прямо сейчас: в алерте
    # сказать, что то же у других нод с теми же резолверами (сбой у провайдера, а не на ноде)
    dns_slow_by: dict[int, set[str]] = {}
    for _s in servers:
        _rep = _s.last_report or {}
        if seen_online(_s, now) and dns_problem(_rep):
            dns_slow_by[_s.id] = dns_suspects(_rep)

    async def cause_for(s: Server, key: str) -> str:
        """«Почему» для порогового алерта: кто ест ресурс и не наплыв ли трафика.
        Норму считаем один раз на сервер за тик. Не прочиталась - алерт уходит без
        хвоста: причина это украшение, терять из-за неё сам алерт нельзя."""
        if s.id not in base_cache:
            try:
                async with session_factory() as ses:
                    base_cache[s.id] = await hour_baseline(ses, s.id, now)
            except Exception:
                log.warning("норма трафика для %s не посчиталась", s.name, exc_info=True)
                base_cache[s.id] = {}
        return cause_text(s.last_report or {}, key, base_cache[s.id], now, pod_names)

    def srv_fire(s: Server, key: str, rule: dict, ctx: dict) -> alerts.Msg:
        """Текст срабатывания: дефолт → богатый формат (иконка + имя-ссылкой + суть),
        кастомный шаблон пользователя → рендерим как есть + строка со ссылкой."""
        url = srv_url(s, key)
        default = settings_store.SERVER_ALERT_KINDS[key][1]
        if rule["text"] == default or rule["text"] in settings_store.LEGACY_SERVER_DEFAULTS:
            # у диска три уровня — ведущая иконка должна их различать
            ico = _DISK_ICON.get(int(ctx.get("level") or 0), "") if key in ("disk", "inode") else ""
            if key == "disk_health":
                ico = _DISK_ICON.get(int(ctx.get("level") or 0), "") + "💽"
            if key == "disk_forecast":
                ico = _DISK_ICON.get(int(ctx.get("level") or 0), "") + "📈"
            if key == "units":
                ico = _DISK_ICON.get(int(ctx.get("level") or 0), "") + "⚙️"
            if key == "port_new":
                ico = "🔴🚪"  # порт открыт всем: открытый только белому списку сюда не доходит
            if key == "kube_expiry" and ctx.get("expired"):
                ico = "⛔"  # срок не «скоро», а уже вышел — это поломка, а не напоминание
            return _server_alert_text(
                key, s.name, _fmt_rule(default, s, ctx), url, icon=ico,
                group=s.group_name or "", ref=f"srv:{s.id}",
            )
        txt = html.escape(_fmt_rule(rule["text"], s, ctx))
        return alerts.Msg(
            txt + (f"\n🔗 {html.escape(url)}" if url else ""),
            _ALERT_SECTION.get(key, "servers"),
            s.group_name or "", kind=key, target=s.name, ref=f"srv:{s.id}",
        )

    # Сначала СОБИРАЕМ все переходы за тик (не шлём внутри цикла), чтобы при
    # массовых событиях схлопнуть их в один дайджест. Состояние фиксируем после
    # доставки: молчаливые де-эскалации — всегда; срабатывания/восстановления —
    # только если их пачка ушла (иначе повторим на следующем тике).
    cur_by_id = {s.id: dict(s.alert_state or {}) for s in servers}
    changed_ids: set[int] = set()
    fires: list[alerts.Msg] = []
    recoveries: list[alerts.Msg] = []
    fire_apply: list[tuple[int, str, object]] = []  # (sid, key, value)
    rec_apply: list[tuple[int, str, object]] = []
    # известные проблемы, которые сборщик обновил или закрыл: (sid, вид, at записи, новая|None)
    known_upd: list[tuple[int, str, str | None, dict | None, str]] = []
    # новые известные проблемы: что было при подключении сервера (sid, вид, запись)
    known_add: list[tuple[int, str, dict]] = []
    # сервер закрыл подключение сам (вышел ONBOARD_MAX): sid -> что стало известным
    onboard_closed: dict[int, list[str]] = {}
    # где в очередях начинается каждый сервер: у серверов на подключении после цикла
    # отправляется только офлайн и критичный диск (onboard_mod.split)
    marks: list[tuple[int | None, int, int, int, int]] = []
    onboarding: set[int] = set()
    probes_left = _LIVENESS_PER_TICK
    live_common = liveness.shared(servers)  # адреса, общие для нескольких серверов: там только SSH

    def apply(sid: int, key: str, value: object) -> None:
        cur_by_id[sid][key] = value
        changed_ids.add(sid)

    for s in servers:
        st = cur_by_id[s.id]
        marks.append((s.id, len(fires), len(fire_apply), len(recoveries), len(rec_apply)))
        # снуз: сервер временно приглушён — алерты не шлём (но состояние не трогаем,
        # чтобы после снуза оставшаяся проблема снова сработала)
        if s.snooze_until is not None and _aware(s.snooze_until) > now:
            continue
        if s.id in maint_srv:
            continue  # идет окно обслуживания: то же, что снуз, после окна оставшееся сработает
        # Только что добавленный сервер: первые минуты ставятся helper'ы, и первые отчеты
        # неполные (у сервера бэкапов "чистка не работает" ушла через минуту после добавления и
        # через минуту отбилась). Состояние не трогаем: что останется, придет после тишины.
        created = getattr(s, "created_at", None)
        if created is not None and 0 <= (now - _aware(created)).total_seconds() < _NEW_SERVER_QUIET:
            continue
        # Идет подключение (app/onboard.py): пороги придерживаются, события молча двигают отметку
        onb = onboard_mod.active(s, now)
        if onb:
            onboarding.add(s.id)
            if not st.get("onboard"):
                apply(s.id, "onboard", 1)
        # заглушённые типы: постоянные мьюты + точечный ВРЕМЕННЫЙ снуз отдельных
        # типов (напр. приглушить OOM на день, но продолжать слать offline/CPU)
        mutes = set(s.alert_mutes or [])
        for k, until in (s.alert_snoozes or {}).items():
            u = _parse_iso(until)
            if u is not None and u > now:
                mutes.add(k)
        # Отбой «мигания» проверяем каждый тик, а не на переключении: успокоившаяся
        # нода переключений и не делает — иначе флаг висел бы вечно.
        if st.get("flap_muted"):
            fs = _parse_iso(st.get("flap_since") or "")
            if fs is None or (now - fs).total_seconds() > _FLAP_WINDOW:
                apply(s.id, "flap_muted", 0)
                apply(s.id, "flap_count", 0)
                recoveries.append(
                    _server_alert_text(
                        "offline", s.name, "связь снова стабильна",
                        srv_url(s, "offline"), recovery=True, group=s.group_name or "",
                    )
                )
        # То же для пороговых метрик: за окно ни одного нового захода - снова
        # разрешаем алерты. Отбой шлём, только если метрика реально вернулась в
        # норму, иначе «снова в норме» было бы неправдой.
        for fk in _FLAP_METRIC_KINDS:
            if not st.get(f"flap_{fk}_muted"):
                continue
            fs = _parse_iso(st.get(f"flap_{fk}_since") or "")
            if fs is not None and (now - fs).total_seconds() <= _FLAP_METRIC_WINDOW:
                continue
            apply(s.id, f"flap_{fk}_muted", 0)
            apply(s.id, f"flap_{fk}_count", 0)
            if int(st.get(fk, 0)) == 0:
                recoveries.append(_server_alert_text(
                    fk, s.name,
                    f"{settings_store.SERVER_ALERT_KINDS[fk][0]} снова в норме",
                    srv_url(s, fk), recovery=True, group=s.group_name or "",
                ))
        conds = _server_conditions(s, now, web_err.get(s.id), pod_names)
        # Нода молчит: про ее CPU, память и диски ничего не известно. Все, кроме самого
        # "недоступен", замирает как было: ни срабатываний, ни отбоев, ни сброса выдержек.
        # Иначе пороговые условия на молчащей ноде падали в ноль, и объявленный CPU получал
        # "снова в норме" ровно тогда, когда нода пропала (и после каждого деплоя панели).
        talking = seen_online(s, now)
        # состояние дебаунса ведём КАЖДЫЙ тик, даже без смены уровня алерта — иначе
        # оно не накопится (при level==prev цикл ниже делает continue без записи).
        # «<тип>_since» — момент начала «жарки» (по времени); throttle — «_streak».
        for k, (_lvl, cx) in conds.items():
            if not talking and k != "offline":
                continue
            if "since" in cx and st.get(f"{k}_since") != cx["since"]:
                apply(s.id, f"{k}_since", cx["since"])
            if "streak" in cx and int(st.get(f"{k}_streak", 0)) != cx["streak"]:
                apply(s.id, f"{k}_streak", cx["streak"])
            # с какого момента подряд тормозит каждый резолвер DNS (для "Что сломано")
            if "res" in cx and (st.get(f"{k}_res") or {}) != cx["res"]:
                apply(s.id, f"{k}_res", cx["res"] or None)
            # гистерезис CPU и RAM: с какого момента значение ниже линии снятия
            if "cool" in cx and st.get(f"{k}_cool") != cx["cool"]:
                apply(s.id, f"{k}_cool", cx["cool"])
            # выдержка отбоя упавших юнитов: с какого момента юнита нет среди упавших
            if "hold" in cx and (st.get(f"{k}_hold") or {}) != cx["hold"]:
                apply(s.id, f"{k}_hold", cx["hold"] or None)
            # с какого момента проблема видна - для "Что сломано" ("sda изношен - 2 дня");
            # ведем для любого вида и до проверки мьютов: заглушенное тоже остается проблемой
            frm = st.get(f"{k}_from")
            if _lvl > 0 and not frm:
                apply(s.id, f"{k}_from", now.isoformat())
            elif _lvl <= 0 and frm:
                apply(s.id, f"{k}_from", None)
        # Известные проблемы (app/known.py): снимок того, что отметили, закрытие, когда ушла
        # сама или вышел срок, и напоминание раз в неделю, что она все еще висит
        known_now: dict[str, dict] = {}
        reminds: list[tuple[str, dict]] = []
        for kk, ent in (s.known_issues or {}).items():
            if not isinstance(ent, dict):
                continue
            if not talking:
                # нода молчит: что с ее диском, неизвестно, и "проблема ушла" было бы неправдой
                known_now[kk] = ent
                continue
            k_lvl, k_ctx = conds.get(kk, (0, {}))
            new_k, what = known_mod.step(ent, k_lvl, k_ctx.get("sig"), now)
            if new_k != ent:
                known_upd.append((s.id, kk, ent.get("at"), new_k, what))
            if new_k is None:
                continue
            known_now[kk] = new_k
            if what == "remind":
                reminds.append((kk, new_k))
        if reminds:
            # Отмеченные разом (что было при подключении) напоминают о себе в один тик: одним
            # сообщением на сервер, а не по сообщению на каждую
            lines = [known_mod.remind_text(settings_store.SERVER_ALERT_KINDS.get(kk, (kk,))[0], e, now)
                     for kk, e in reminds]
            fires.append(_server_alert_text(
                "known", s.name, lines[0] if len(lines) == 1 else "; ".join(lines),
                srv_url(s, reminds[0][0]), icon="📌", group=s.group_name or ""))
        # Подключение закрыто (человеком или само): что все еще сломано и не объявлялось,
        # становится известной проблемой. Алерт придет, если станет хуже или сломается новое
        if not onb and st.get("onboard") and talking:
            made = []
            for key, (level, k_ctx) in conds.items():
                if key == "offline" or level <= 0 or int(st.get(key, 0)) or key in known_now:
                    continue
                rule = rules.get(key) or _fallback_rule(key)
                if not rule["enabled"] or not _rule_scope_ok(rule, s) or _muted(key, level, mutes):
                    continue
                ent = onboard_mod.known_entry(s.onboard_by, now, level, k_ctx.get("sig"))
                known_now[key] = ent
                known_add.append((s.id, key, ent))
                made.append(key)
            apply(s.id, "onboard", 0)
            if s.onboard_done_at is None:
                onboard_closed[s.id] = made
        for key, (level, ctx) in conds.items():
            if not talking and key != "offline":
                continue  # нода молчит: остальное замерло (см. выше)
            rule = rules.get(key) or _fallback_rule(key)
            if not rule["enabled"] or not _rule_scope_ok(rule, s):
                continue  # правило выключено или сервер вне области применения
            if key == "offline" and level > int(st.get(key, 0)) and s.parent_id \
                    and (s.parent_id in silent or s.parent_id in maint_srv):
                # стоит за молчащим (или обслуживаемым) сервером: молчит из-за него, это строка в
                # его сообщении. Состояние не трогаем: родитель вернулся, а этот все еще молчит -
                # придет свой "недоступен"
                continue
            if _muted(key, level, mutes):
                continue  # тип (или его нижние уровни) заглушён для этого сервера
            if known_mod.covers(known_now.get(key), level, ctx.get("sig")):
                continue  # известная проблема: про нее знают, молчим, пока не стало хуже
            if onb and not onboard_mod.cond_ok(key, level) and level > int(st.get(key, 0)):
                # идет подключение: придерживаем без записи состояния. Видно в карточке и в "Что
                # сломано"; что останется к закрытию, станет известной проблемой
                continue
            prev = int(st.get(key, 0))
            if key in ("disk_health", "units", "docker_restarts", "kube_restarts", "mem_leak", "db_health",
                       "web_config", "web_cert", "listen_gone", "docker_risk", "access_risk", "link", "gpu",
                       "queue_stuck", "queue_failed", "workers", "cron_stuck", "log_errors", "fpm"):
                # Тот же уровень, но сломалось НОВОЕ (второй диск при уже развалившемся
                # RAID, второй упавший юнит): молчать нельзя, хотя уровень не вырос. Что
                # сломано - ключи в sig.
                sk = f"{key}_sig"
                tk = f"{key}_told"
                old_sig = set(st.get(sk) or [])
                new_sig = set(ctx.get("sig") or [])
                if key == "kube_restarts":
                    # sig был по подам, стал по ворклоадам: та же беда под новым ключом не новость
                    old_sig = {podnames.pod_key(x) for x in old_sig}
                if key == "log_errors":
                    # то же с логами подов (1.4.278): копии ворклоада - один лог
                    old_sig = {log_errors.norm_key(x) for x in old_sig}
                # О чем уже сказали, пока алерт открыт: sig не только сейчас, но и все, что в нем
                # было. Пятнадцать копий воркера уходили из беды и возвращались по очереди, и каждое
                # возвращение было "новым": десять сообщений за полтора часа об одном и том же
                told = set(st.get(tk) or []) | old_sig if prev else set()
                if key == "workers":
                    # до 1.4.273 копии программы были отдельными ключами: о программе уже сказано
                    told |= {workers_mod.program_key(x) for x in told}
                if key == "log_errors":
                    told |= {log_errors.norm_key(x) for x in told}
                if prev and not level and old_sig and _sig_all_quiet(s, key, old_sig, now):
                    # объявленное теперь заглушено (или это свой helper, по которому алерта
                    # больше нет): ничего не стихло, отбой был бы неправдой
                    rec_apply.append((s.id, sk, []))
                    rec_apply.append((s.id, tk, []))
                    rec_apply.append((s.id, key, 0))
                    continue
                if level == prev:
                    if level and new_sig - told:
                        said = _parse_iso(str(st.get(f"{key}_said") or ""))
                        if key in _SIG_STORY and said is not None and (now - said).total_seconds() < _SIG_STORY_S:
                            # Перезапуски считаются за сутки: еще один деплоймент перешел порог
                            # через минуту после сообщения - та же история (на одной ноде
                            # два сообщения подряд), он виден в "Что сломано"
                            apply(s.id, sk, sorted(new_sig))
                            apply(s.id, tk, sorted(told | new_sig))
                        else:
                            fires.append(srv_fire(s, key, rule, ctx))
                            fire_apply.append((s.id, sk, sorted(new_sig)))
                            fire_apply.append((s.id, tk, sorted(told | new_sig)))
                            fire_apply.append((s.id, f"{key}_said", now.isoformat()))
                    elif new_sig != old_sig:
                        apply(s.id, sk, sorted(new_sig))
                        if level and not st.get(tk):
                            apply(s.id, tk, sorted(told))
                    continue
                if level > prev:
                    fire_apply.append((s.id, sk, sorted(new_sig)))
                    fire_apply.append((s.id, tk, sorted(told | new_sig)))
                    fire_apply.append((s.id, f"{key}_said", now.isoformat()))
                elif level:
                    apply(s.id, sk, sorted(new_sig))
                    apply(s.id, tk, sorted(told | new_sig))
                else:
                    rec_apply.append((s.id, sk, []))
                    rec_apply.append((s.id, tk, []))
            if key in _ROUTINE_KINDS:
                # Распорядок (см. _ROUTINE_KINDS): тот же эпизод в то же время каждый день не алертит.
                # До проверки "уровень тот же": придержанный эпизод, который затянулся, - новость
                now_ts = now.timestamp()
                quiet = st.get(f"{key}_quiet")
                hist = st.get(f"{key}_hist") or []
                if level and not prev:
                    hist = routine_start(hist, now_ts)
                    apply(s.id, f"{key}_hist", hist)
                    r = routine_of(hist[:-1], now_ts)
                    if r:
                        ptz = cfg.get("tz") or "Europe/Moscow"
                        usual = "-".join(tg_buttons.hhmm(datetime.fromtimestamp(x, timezone.utc), ptz, now) for x in r)
                        apply(s.id, key, level)
                        apply(s.id, f"{key}_quiet", r[1] + _ROUTINE_OVER)
                        apply(s.id, f"{key}_usual", usual)
                        continue
                elif level and quiet:
                    if now_ts < float(quiet):
                        if level != prev:
                            apply(s.id, key, level)
                        continue
                    # держится дольше обычного: теперь это новость
                    apply(s.id, f"{key}_quiet", None)
                    usual = str(st.get(f"{key}_usual") or "")
                    ctx["_note"] = (f"обычно так бывает в {usual} и проходит само, сегодня держится дольше" if usual
                                    else "держится дольше обычного")
                    prev = 0
                elif not level and prev:
                    apply(s.id, f"{key}_hist", routine_end(hist, now_ts))
                    if quiet:
                        apply(s.id, key, 0)
                        apply(s.id, f"{key}_quiet", None)
                        continue
            if level == prev:
                continue
            # Гаситель «мигания» — только для НОВЫХ обрывов. Восстановление после уже
            # ОБЪЯВЛЕННОГО обрыва шлём всегда: иначе счётчик переключений добирает порог
            # именно на возврате, ✅ съедается, и в ленте навсегда остаётся 🔥 — сервер
            # выглядит лежащим, хотя вернулся минуту спустя.
            said = key == "offline" and bool(st.get("offline_said"))
            if key == "offline" and not (level == 0 and said) \
                    and _flapping(s, st, now, apply, fires, srv_url(s, "offline")):
                # нода «прыгает» — про каждый скачок больше не пишем. Одно сообщение
                # про нестабильность уже отправлено, дальше ждём, пока успокоится.
                fire_apply.append((s.id, key, level))
                continue
            if level > prev:  # срабатывание/эскалация (напр. warn→crit)
                # Метрика ходит вокруг порога - одно сообщение на серию. Считаем
                # только заходы с нуля: эскалация (диск warn→crit) это растущая
                # проблема, про неё молчать нельзя.
                if prev == 0 and key in _FLAP_METRIC_KINDS and _flap_metric(
                    s, st, now, apply, fires, srv_url(s, key), key,
                    settings_store.SERVER_ALERT_KINDS[key][0],
                ):
                    fire_apply.append((s.id, key, level))
                    continue
                if key in ("cpu", "mem"):
                    ctx["cause"] = await cause_for(s, key)
                if key == "pressure":
                    # кто ест память или какой диск занят: главное, что спросит инженер
                    if ctx.get("res") == "mem":
                        ctx["cause"] = await cause_for(s, "mem")
                    elif ctx.get("res") == "io":
                        ctx["cause"] = io_cause(s.last_report or {})
                    if ctx.get("hint"):
                        # зависший процесс: что это значит и куда смотреть - второй строкой
                        ctx["cause"] = (ctx.get("cause") or "") + "\n↳ " + ctx["hint"]
                if key == "dns":
                    ctx["details"] = (ctx.get("details") or "") + dns_others_hint(s.id, dns_slow_by)
                if key == "disk":
                    # на что ушло место - из разбора helper'а diskusage-setup, без запросов
                    ctx["cause"] = disk_cause(s.last_report or {}, s.disk_forecast, ctx.get("mount"), now)
                if key == "inode":
                    ctx["cause"] = inode_cause(s.last_report or {}, s.disk_forecast, ctx.get("mount"), now)
                if key == "web_5xx":
                    try:
                        async with session_factory() as ses:
                            ctx["where"] = web_where_text(await web_error_where(
                                ses, s.id, now, web_muted_keys(s, now)))
                    except Exception:
                        log.warning("не собрал, где ошибки 5xx на %s", s.name, exc_info=True)
                fmsg = srv_fire(s, key, rule, ctx)
                if ctx.get("_note"):
                    fmsg = alerts.Msg(fmsg + "\n↳ " + html.escape(str(ctx["_note"])), fmsg.section, fmsg.group,
                                      fmsg.kind, fmsg.target, fmsg.recovery, fmsg.ref)
                if key == "offline" and (tail := deps_tail(s.id)):
                    # что молчит и лежит из-за него: их собственные алерты придержаны
                    fmsg = alerts.Msg(fmsg + tail, fmsg.section, fmsg.group, fmsg.kind, fmsg.target,
                                      fmsg.recovery, fmsg.ref)
                if key == "offline" and probes_left > 0:
                    # молчит агент или сам сервер: панель стучится к нему сама (app/liveness.py)
                    probes_left -= 1
                    try:
                        live = await liveness.check(s, common=live_common.get(s.id, frozenset()))
                    except Exception:
                        log.warning("не проверил, жив ли %s", s.name, exc_info=True)
                        live = None
                    if live:
                        fmsg = alerts.Msg(fmsg + "\n↳ " + html.escape(liveness.text(live)), fmsg.section,
                                          fmsg.group, fmsg.kind, fmsg.target, fmsg.recovery, fmsg.ref)
                        fire_apply.append((s.id, "offline_probe", live | {"ts": now.isoformat()}))
                if key in ("offline", "web_5xx"):
                    # что менялось на сервере за час до сбоя: деплой, перезагрузка, пакеты
                    fmsg = await _with_before(session_factory, fmsg, s,
                                              _aware(s.last_seen) if key == "offline" and s.last_seen else now)
                fires.append(fmsg)
                fire_apply.append((s.id, key, level))
                if key in _SRV_UNIT and ctx.get("value") is not None:
                    # запоминаем ОБЪЯВЛЕННОЕ значение — из него отбой построит «было N».
                    # При эскалации перезаписываем: сравнивать надо с последним, что
                    # человек видел в ленте, а не с первым срабатыванием
                    fire_apply.append((s.id, f"{key}_val", ctx["value"]))
                if key == "offline":
                    # «обрыв объявлен» ставим вместе с самим алертом (тот же fire_apply),
                    # чтобы флаг не появился, когда отправка пачки не удалась
                    fire_apply.append((s.id, "offline_said", 1))
            elif level == 0:  # полное восстановление
                if key in _FLAP_METRIC_KINDS and st.get(f"flap_{key}_muted"):
                    apply(s.id, key, 0)  # серия заглушена: про мигание уже сказано
                    continue
                detail = (
                    "снова доступен"
                    if key == "offline"
                    else _recovery_detail(key, st, ctx)
                )
                recoveries.append(
                    _server_alert_text(key, s.name, detail, srv_url(s, key), recovery=True, group=s.group_name or "")
                )
                rec_apply.append((s.id, key, 0))
                if key == "offline":
                    rec_apply.append((s.id, "offline_said", 0))
                    rec_apply.append((s.id, "offline_probe", None))
            else:  # де-эскалация (crit→problem): без алерта, просто снижаем уровень
                apply(s.id, key, level)
        # перезагрузка (аптайм сброшен) — одноразовый алерт на новый rebooted_at
        rb = rules.get("reboot")
        if s.rebooted_at is not None and "reboot" not in mutes and rb and rb["enabled"] and _rule_scope_ok(rb, s):
            stamp = _aware(s.rebooted_at).isoformat()
            if st.get("reboot_at") != stamp:
                fires.append(srv_fire(s, "reboot", rb, {}))
                fire_apply.append((s.id, "reboot_at", stamp))
        # Новый порт открылся в интернет - событие на каждую проверку, нашедшую новые (new_ts).
        # Заглушенный или выключенный алерт отметку тоже двигает: иначе после снятия мьюта
        # пришла бы старая новость
        pstamp = open_ports.new_since(s)
        if pstamp and st.get("port_new_at") != pstamp:
            pn, fresh = rules.get("port_new"), open_ports.new_ports(s, now)
            if fresh and "port_new" not in mutes and pn and pn["enabled"] and _rule_scope_ok(pn, s):
                fires.append(srv_fire(s, "port_new", pn, {
                    "detail": "в интернет открылся новый порт: " + open_ports.new_text(fresh)}))
                fire_apply.append((s.id, "port_new_at", pstamp))
            else:
                apply(s.id, "port_new_at", pstamp)
        # Изменился доступ (helper changes-setup) - событие на каждый снимок с изменениями доступа.
        # Без прежней отметки говорим только о свежем: иначе после сброса состояния пришла бы
        # вся лента за три месяца
        astamp = (s.changes or {}).get("alert_ts") if isinstance(s.changes, dict) else None
        if astamp and st.get("access_change_at") != astamp:
            after = st.get("access_change_at") or (now - timedelta(hours=1)).isoformat()
            fresh_ev = srv_changes.alert_events(s, after)
            ac = rules.get("access_change")
            if fresh_ev and "access_change" not in mutes and ac and ac["enabled"] and _rule_scope_ok(ac, s):
                fires.append(srv_fire(s, "access_change", ac, {
                    "detail": "изменился доступ: " + srv_changes.events_text(fresh_ev, 6)}))
                fire_apply.append((s.id, "access_change_at", astamp))
            else:
                apply(s.id, "access_change_at", astamp)
        # Упавшие разовые Job кластера - событие на каждый новый провал. Первое знакомство
        # задает точку отсчета: упавшее за прошлую неделю не шлем пачкой после обновления
        # (агент без failed_jobs в отчете: пустой список он не шлет, старше 2.26 не шлет вовсе)
        k_rep = s.last_report or {}
        if (k_rep.get("kube") or {}).get("access") and _ver_tuple(k_rep.get("agent_version")) >= (2, 26):
            jobs = kube_failed_jobs(k_rep)
            keys = sorted(job_logs.job_key(j) for j in jobs)
            seen = st.get("kube_jobs_seen")
            if seen is None:
                apply(s.id, "kube_jobs_seen", keys)
            elif set(keys) - set(seen):
                fresh = [j for j in jobs if job_logs.job_key(j) not in set(seen)]
                kj = rules.get("kube_job")
                if "kube_job" not in mutes and kj and kj["enabled"] and _rule_scope_ok(kj, s):
                    # хвост лога пода просит стадия "логи упавших Job"; пока агент не ответил
                    # (до job_logs._WAIT), алерт ждет следующего тика, kube_jobs_seen не трогаем
                    logs = job_logs.collect(s.id, k_rep, fresh, now.timestamp())
                    if logs is not None:
                        fires.append(job_logs.attach(srv_fire(s, "kube_job", kj, {
                            "detail": ("упал Job: " if len(fresh) == 1 else "упали Job: ")
                            + failed_jobs_text(fresh, 5)}), logs))
                        fire_apply.append((s.id, "kube_jobs_seen", keys))
                else:
                    apply(s.id, "kube_jobs_seen", keys)
            elif sorted(seen) != keys:
                apply(s.id, "kube_jobs_seen", keys)  # старые ушли из списка
        # OOM-kill — алерт по КУМУЛЯТИВНОМУ счётчику (oom_total копится в ingest'е).
        # High-water mark oom_seen в alert_state: любой рост → алерт (не теряем килл,
        # даже если поллинг не совпал с отчётом). Событие, не состояние → без recovery.
        oom_total = s.oom_total or 0
        oom_seen = int(st.get("oom_seen", 0))
        om = rules.get("oom")
        if (oom_total > oom_seen and "oom" not in mutes and om and om["enabled"]
                and _rule_scope_ok(om, s)):
            victim = (s.oom_victim or "").strip()
            ctx = {"value": oom_total - oom_seen, "victim": f" · {victim}" if victim else ""}
            fires.append(srv_fire(s, "oom", om, ctx))
            fire_apply.append((s.id, "oom_seen", oom_total))

        # Docker-контейнеры: crash-loop (RestartCount растёт) и «упал» (не running при
        # наличии restart-policy, держится ≥ sustain). Многоинстансно — своё состояние
        # на контейнер в alert_state["docker"][name]. Только онлайн-серверы с доступом к
        # сокету (у оффлайна данные протухли — это покрывает offline-алерт).
        dk = (s.last_report or {}).get("docker") or {}
        online_now = seen_online(s, now)
        if dk.get("access") and online_now and (dk.get("containers") or []):
            r_down, r_loop = rules.get("docker_down"), rules.get("docker_loop")
            _ss = getattr(s, "alert_sustain_seconds", None)
            sustain_s = _SUSTAIN_DEFAULT if _ss is None else max(int(_ss), 0)
            dstate = dict(st.get("docker") or {})
            seen_names: set[str] = set()
            for c in dk.get("containers") or []:
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                seen_names.add(name)
                cs = dict(dstate.get(name) or {})
                rc = int(c.get("restarts") or 0)
                state = (c.get("state") or "").lower()
                policy = (c.get("policy") or "").lower()
                # crash-loop: копим моменты прироста RestartCount, чистим старше окна
                loops = [
                    ts for ts in (cs.get("loops") or [])
                    if (p := _parse_iso(ts)) and (now - p).total_seconds() <= _DOCKER_LOOP_WINDOW
                ]
                prev_rc = cs.get("rc")
                if prev_rc is not None and rc > int(prev_rc):
                    loops.append(now.isoformat())  # был ≥1 рестарт с прошлого тика
                cs["rc"], cs["loops"] = rc, loops
                looping = len(loops) >= _DOCKER_LOOP_MIN
                was_loop = bool(cs.get("alerted_loop"))
                loop_ok = r_loop and r_loop["enabled"] and "docker_loop" not in mutes and _rule_scope_ok(r_loop, s)
                if looping and not was_loop and loop_ok:
                    ctx = {"container": name, "restarts": len(loops),
                           "window": round(_DOCKER_LOOP_WINDOW / 60), "policy": policy or "no", "state": state}
                    fires.append(srv_fire(s, "docker_loop", r_loop, ctx))
                    cs["alerted_loop"] = True
                elif was_loop and not loops:  # рестарты прекратились
                    # Отметку снимаем всегда, отбой шлем только по живому правилу: иначе контейнер,
                    # заглушенный посреди алерта, навсегда оставался "в цикле" на главной.
                    if loop_ok:
                        recoveries.append(_server_alert_text(
                            "docker_loop", s.name, f"контейнер {name}: перезапуски прекратились",
                            srv_url(s, "docker_loop"), recovery=True, group=s.group_name or ""))
                    cs["alerted_loop"] = False
                # «упал»: не работает и это авария (docker_down_why: не остановлен руками),
                # держится ≥ sustain
                why = docker_down_why(c)
                is_down = why is not None
                was_down = bool(cs.get("alerted_down"))
                down_ok = r_down and r_down["enabled"] and "docker_down" not in mutes and _rule_scope_ok(r_down, s)
                if is_down:
                    since = cs.get("down_since") or now.isoformat()
                    cs["down_since"] = since
                    started = _parse_iso(since)
                    held = (now - started).total_seconds() if started else 0.0
                    if held >= sustain_s and not was_down and down_ok:
                        # перед этим - без событий самого контейнера: что он упал, и так понятно
                        fires.append(await _with_before(session_factory, srv_fire(
                            s, "docker_down", r_down,
                            {"container": name, "state": state, "policy": policy or "no", "why": why},
                        ), s, started or now, frozenset({name})))
                        cs["alerted_down"] = True
                else:
                    cs["down_since"] = None
                    if was_down:  # снова поднялся; отметку снимаем и у заглушенного
                        if down_ok:
                            # не поднялся, но это уже не авария: остановили руками, сняли policy
                            back = (f"контейнер {name} снова работает" if state == "running"
                                    else f"контейнер {name} остановлен штатно, тревога снята")
                            recoveries.append(_server_alert_text(
                                "docker_down", s.name, back, srv_url(s, "docker_down"), recovery=True, group=s.group_name or ""))
                        cs["alerted_down"] = False
                dstate[name] = cs
            # контейнеры, пропавшие из списка (удалены/пересозданы) — чистим состояние
            for gone in [n for n in dstate if n not in seen_names]:
                dstate.pop(gone, None)
            if dstate != (st.get("docker") or {}):
                apply(s.id, "docker", dstate)

        # Юниты systemd в цикле перезапусков (helper units-setup 0.2). Состояние - на каждый юнит
        # в alert_state["unit_loops"]: приросты счетчика за окно и отметка, что алерт ушел. Блок
        # протух или helper старый - состояние не трогаем: молчание не значит, что цикл кончился.
        rs = units_restarting(s.last_report or {}) if online_now else None
        if rs is not None:
            ul_rule = rules.get("unit_loop")
            ul_ok = bool(ul_rule and ul_rule["enabled"] and "unit_loop" not in mutes and _rule_scope_ok(ul_rule, s))
            muted_u = units_muted(s, now)
            ustate = dict(st.get("unit_loops") or {})
            seen_u: set[str] = set()
            for u in rs:
                name = str(u["unit"])
                seen_u.add(name)
                cs = unit_loop_step(ustate.get(name), _int(u.get("n")), now)
                hits = cs["hits"]
                quiet = name in muted_u or not ul_ok
                if len(hits) >= _UNIT_LOOP_MIN and not cs["alerted"] and not quiet:
                    count, why = unit_loop_text(u, hits)
                    fires.append(srv_fire(s, "unit_loop", ul_rule, {
                        "unit": name, "count": count, "window": _UNIT_LOOP_WINDOW // 60, "why": why}))
                    cs["alerted"] = True
                elif cs["alerted"] and not hits:
                    # отметку снимаем всегда, отбой шлем только по живому правилу
                    if not quiet:
                        recoveries.append(_server_alert_text(
                            "unit_loop", s.name, f"{name}: перезапуски прекратились",
                            srv_url(s, "unit_loop"), recovery=True, group=s.group_name or ""))
                    cs["alerted"] = False
                ustate[name] = cs
            # юнит пропал из списка: счетчик обнулили ручным restart, юнит убрали, перезагрузка
            for gone in [n for n in ustate if n not in seen_u]:
                cs = ustate.pop(gone)
                if cs.get("alerted") and ul_ok and gone not in muted_u:
                    recoveries.append(_server_alert_text(
                        "unit_loop", s.name, f"{gone}: перезапуски прекратились",
                        srv_url(s, "unit_loop"), recovery=True, group=s.group_name or ""))
            if ustate != (st.get("unit_loops") or {}):
                apply(s.id, "unit_loops", ustate)

        # Счетчики перезапусков контейнеров и подов за сутки (restarts): по ним условия
        # docker_restarts и kube_restarts на следующем тике
        if online_now:
            rstate = srv_restarts.update(st.get("restarts"), s.last_report or {}, now)
            if rstate != (st.get("restarts") or {}):
                apply(s.id, "restarts", rstate)
            # история слушающих портов: какой порт держится неделями и пропал (listen_watch)
            lstate = listen_watch.update(st.get("listen_hist"), s.last_report or {}, now)
            if lstate is not None and lstate != (st.get("listen_hist") or {}):
                apply(s.id, "listen_hist", lstate)
            # суточные минимумы памяти контейнеров, подов и процессов (mem_trend)
            mstate = mem_trend.update(st.get("mem_trend"), s.last_report or {}, now)
            if mstate != (st.get("mem_trend") or {}):
                apply(s.id, "mem_trend", mstate)
            # ход очередей: растет и не разбирается (queue_watch)
            if queue_watch.queues(s.last_report or {}) or st.get("qwatch"):
                qw = queue_watch.update(st.get("qwatch"), s.last_report or {}, now)
                if qw != (st.get("qwatch") or {}):
                    apply(s.id, "qwatch", qw)
            # обычные задержки LLM-сервисов: от них алерт "задержки выросли в разы" (gpu.base_update)
            if gpu_mod.llm_list(s.last_report or {}) or st.get("llm_base"):
                bstate = gpu_mod.base_update(st.get("llm_base"), s.last_report or {}, now)
                if bstate != (st.get("llm_base") or {}):
                    apply(s.id, "llm_base", bstate)

        # Очереди RabbitMQ: алертим те, что переросли свой порог. Состояние — на
        # КАЖДУЮ очередь: очередей десятки, и одна разбухшая не должна глушить
        # остальные, а вернувшаяся в норму обязана дать отбой сама по себе.
        qr = rules.get("queue")
        if online_now and qr and qr["enabled"] and "queue" not in mutes and _rule_scope_ok(qr, s):
            qstate = dict(st.get("queues") or {})
            seen_q: set[str] = set()
            qmuted = queue_watch.muted_keys(s, now)
            for b in queue_watch.brokers(s.last_report or {}):
                src = b["source"]
                for q in b["queues"]:
                    if not isinstance(q, dict):
                        continue
                    key = queue_key(src, q)
                    seen_q.add(key)
                    thr = queue_threshold(s, key)
                    # порога нет — очередь не сторожим; заглушенная (насовсем или на время) молчит,
                    # состояние держим: после мьюта переполненная не придет второй раз
                    if thr <= 0 or key in qmuted:
                        continue
                    depth = queue_depth(q)
                    was = bool(qstate.get(key))
                    if depth >= thr and not was:
                        ctx = {"queue": q.get("name") or "?", "source": src or "rabbitmq",
                               "value": depth, "threshold": thr}
                        fires.append(srv_fire(s, "queue", qr, ctx))
                        qstate[key] = True
                    elif depth < thr and was:
                        recoveries.append(_server_alert_text(
                            "queue", s.name,
                            f"очередь {q.get('name') or '?'} ({src or 'rabbitmq'}) "
                            f"снова в норме: {depth} < {thr}",
                            srv_url(s, "queue"), recovery=True, group=s.group_name or ""))
                        qstate[key] = False
            # очередь удалили/переименовали — состояние по ней больше не нужно
            for gone in [k for k in qstate if k not in seen_q]:
                qstate.pop(gone, None)
            if qstate != (st.get("queues") or {}):
                apply(s.id, "queues", qstate)

        # Ротация: старейший снапшот пережил политику хранения. Отдельно от backup_repo —
        # там про свежесть и целостность, тут про то, что старое НЕ убирается.
        rot_r = rules.get("backup_rotation")
        bsrv_rot = (s.last_report or {}).get("backup_server") or {}
        if (online_now and bsrv_rot.get("present") and rot_r and rot_r["enabled"]
                and "backup_rotation" not in mutes and _rule_scope_ok(rot_r, s)):
            rot_ext = bsrv_extra(s.last_report or {})
            rot_arch = s.backup_repo_archive or {}
            over, seen_over = rotation_overflow(
                bsrv_rot, dict(st.get("rotation_over") or {}), now, rot_ext, rot_arch)
            if seen_over != (st.get("rotation_over") or {}):
                apply(s.id, "rotation_over", seen_over)
            # одна строка на репозиторий: старье и "удалений нет" у него обычно одна беда
            stale = merge_rotation([x for x in rotation_stale_repos(bsrv_rot, now, rot_ext, rot_arch) + over
                                    if x.split(" (")[0] not in set(s.backup_repo_mutes or [])])
            was_stale = bool(st.get("rotation_stale"))
            if stale and not was_stale:
                shown = ", ".join(stale[:6]) + (f" и ещё {len(stale) - 6}" if len(stale) > 6 else "")
                fires.append(srv_fire(s, "backup_rotation", rot_r, {"repos": shown}))
                fire_apply.append((s.id, "rotation_stale", 1))
            elif not stale and was_stale:
                recoveries.append(_server_alert_text(
                    "backup_rotation", s.name, "ротация снова вычищает старые снапшоты",
                    srv_url(s, "backup_rotation"), recovery=True, group=s.group_name or ""))
                rec_apply.append((s.id, "rotation_stale", 0))

        # Висячий лок дольше суток: чистка и проверка репозитория не идут. Раньше он был
        # одной из причин backup_repo и терялся в общем списке, без возраста и без того,
        # что из него следует. Шлем при появлении нового залоченного и раз в сутки, пока
        # лок не снят.
        bsrv = (s.last_report or {}).get("backup_server") or {}
        lr = rules.get("backup_lock")
        if online_now and bsrv.get("present") and "backup_lock" not in mutes:
            locks = long_locked_repos(bsrv, set(s.backup_repo_mutes or []), now)
            names = [n for n, _d in locks]
            prev = st.get("backup_locks") if isinstance(st.get("backup_locks"), dict) else {}
            prev_names = list(prev.get("repos") or [])
            prev_ts = float(prev.get("ts") or 0)
            lock_ok = lr and lr["enabled"] and _rule_scope_ok(lr, s)
            due = bool(set(names) - set(prev_names)) or now.timestamp() - prev_ts >= _BACKUP_REPO_REALERT
            if locks and lock_ok and due:
                items = [f"{n} ({d} дн)" for n, d in locks]
                shown = ", ".join(items[:6]) + (f" и еще {len(items) - 6}" if len(items) > 6 else "")
                fires.append(srv_fire(s, "backup_lock", lr, {"repos": shown}))
                fire_apply.append((s.id, "backup_locks", {"repos": names, "ts": now.timestamp()}))
            elif locks and prev_names and names != prev_names:
                # набор сузился: запоминаем молча, иначе вернувшийся лок не считался бы новым
                apply(s.id, "backup_locks", {"repos": names, "ts": prev_ts})
            elif not locks and prev_names:
                recoveries.append(_server_alert_text(
                    "backup_lock", s.name, "висячих локов в репозиториях больше нет",
                    srv_url(s, "backup_lock"), recovery=True, group=s.group_name or ""))
                rec_apply.append((s.id, "backup_locks", {}))

        # Не прошла недельная проверка целостности репозитория. Как и с локом: при новом
        # сломанном и раз в сутки, пока не починят; отбой, когда все проверки снова прошли.
        cr = rules.get("backup_check")
        if online_now and bsrv.get("present") and "backup_check" not in mutes:
            bad = failed_checks(bsrv, bsrv_extra(s.last_report or {}), set(s.backup_repo_mutes or []), now)
            prev = st.get("backup_checks") if isinstance(st.get("backup_checks"), dict) else {}
            prev_names = list(prev.get("repos") or [])
            prev_ts = float(prev.get("ts") or 0)
            due = bool(set(bad) - set(prev_names)) or now.timestamp() - prev_ts >= _BACKUP_REPO_REALERT
            if bad and cr and cr["enabled"] and _rule_scope_ok(cr, s) and due:
                shown = ", ".join(bad[:6]) + (f" и еще {len(bad) - 6}" if len(bad) > 6 else "")
                fires.append(srv_fire(s, "backup_check", cr, {"repos": shown}))
                fire_apply.append((s.id, "backup_checks", {"repos": bad, "ts": now.timestamp()}))
            elif bad and prev_names and bad != prev_names:
                apply(s.id, "backup_checks", {"repos": bad, "ts": prev_ts})
            elif not bad and prev_names:
                recoveries.append(_server_alert_text(
                    "backup_check", s.name, "проверка целостности репозиториев снова проходит",
                    srv_url(s, "backup_check"), recovery=True, group=s.group_name or ""))
                rec_apply.append((s.id, "backup_checks", {}))

        # Чистка репозитория не работает: не открывает репозиторий, падает, пропущена или не
        # запускается. Как с локом: при новом и раз в сутки, пока не починят. Упавшую - только если
        # упал и следующий прогон (_PRUNE_FAIL_HOLD): разовый сбой проходит сам.
        pr = rules.get("backup_prune")
        if online_now and bsrv.get("present") and "backup_prune" not in mutes:
            cur = prune_problems(bsrv, bsrv_extra(s.last_report or {}), now,
                                 set(s.backup_repo_mutes or []), s.backup_repo_archive or {})
            held = st.get("prune_fail") if isinstance(st.get("prune_fail"), dict) else {}
            fails = {n: float(held.get(n) or now.timestamp()) for n, (k, _r) in cur.items() if k == "failed"}
            if fails != held:
                apply(s.id, "prune_fail", fails)
            shown_now = {n: r for n, (k, r) in cur.items()
                         if k != "failed" or now.timestamp() - fails[n] >= _PRUNE_FAIL_HOLD}
            names = sorted(shown_now)
            prev = st.get("backup_prunes") if isinstance(st.get("backup_prunes"), dict) else {}
            prev_names = list(prev.get("repos") or [])
            prev_ts = float(prev.get("ts") or 0)
            due = bool(set(names) - set(prev_names)) or now.timestamp() - prev_ts >= _BACKUP_REPO_REALERT
            if names and pr and pr["enabled"] and _rule_scope_ok(pr, s) and due:
                items = [f"{n} ({shown_now[n]})" for n in names]
                shown = ", ".join(items[:6]) + (f" и еще {len(items) - 6}" if len(items) > 6 else "")
                fires.append(srv_fire(s, "backup_prune", pr, {"repos": shown}))
                fire_apply.append((s.id, "backup_prunes", {"repos": names, "ts": now.timestamp()}))
            elif names and prev_names and names != prev_names:
                apply(s.id, "backup_prunes", {"repos": names, "ts": prev_ts})
            elif not names and prev_names:
                recoveries.append(_server_alert_text(
                    "backup_prune", s.name, "чистка репозиториев снова работает",
                    srv_url(s, "backup_prune"), recovery=True, group=s.group_name or ""))
                rec_apply.append((s.id, "backup_prunes", {}))

        # Новый клиент бэкап-сервера без агента в панели. Алерт только про новых: тех, кто
        # уже писал сюда, когда проверка появилась, запоминаем молча (их видно в окне
        # бэкап-сервера пометкой "нет в панели"), иначе первое сообщение было бы на 60 строк.
        ur = rules.get("backup_unmonitored")
        if online_now and bsrv.get("present") and "backup_unmonitored" not in mutes:
            cur_un = backup_unmonitored(bsrv, set(s.backup_repo_mutes or []), panel_names, now,
                                        s.backup_repo_archive or {})
            known = st.get("bsrv_clients")
            if not isinstance(known, list):
                apply(s.id, "bsrv_clients", cur_un)
            else:
                new = [n for n in cur_un if n not in set(known)]
                if new and ur and ur["enabled"] and _rule_scope_ok(ur, s):
                    shown = ", ".join(new[:6]) + (f" и еще {len(new) - 6}" if len(new) > 6 else "")
                    fires.append(srv_fire(s, "backup_unmonitored", ur, {"repos": shown}))
                    fire_apply.append((s.id, "bsrv_clients", cur_un))
                elif cur_un != sorted(known):
                    apply(s.id, "bsrv_clients", cur_un)

        # Бэкап-сервер: репозитории, требующие внимания (устарели/битые), кроме
        # заглушенных. Алертим при появлении НОВЫХ проблемных, recovery - когда все чисты.
        rr = rules.get("backup_repo")
        if online_now and bsrv.get("present") and "backup_repo" not in mutes:
            repo_ext = bsrv_extra(s.last_report or {})
            probs = _backup_problem_repos(bsrv, set(s.backup_repo_mutes or []), now, repo_ext,
                                          panel_names, s.backup_repo_archive or {})
            prev = st.get("backup_repos")
            if isinstance(prev, list):  # старый формат (голый список) → мигрируем
                prev = {"repos": prev, "ts": 0}
            prev = prev if isinstance(prev, dict) else {}
            prev_active = bool(prev.get("repos"))
            prev_ts = float(prev.get("ts") or 0)
            repo_ok = rr and rr["enabled"] and _rule_scope_ok(rr, s)
            # НЕ срочный: шлём при ПОЯВЛЕНИИ проблемы, потом напоминаем не чаще раза в сутки —
            # а НЕ на каждое изменение набора (набор флапал по локам -> был спам). Новый
            # проблемный репозиторий шлем сразу: опоздавший бэкап клиента без агента иначе
            # ждал бы суточного напоминания про чужую проблему. Локи тут давно не участвуют.
            new_names = set(probs) - set(prev.get("repos") or [])
            due = not prev_active or bool(new_names) or (now.timestamp() - prev_ts >= _BACKUP_REPO_REALERT)
            if probs and repo_ok and due:
                n = len(probs)
                # с причиной у каждого: по голому списку имен было не понять, что делать
                byname = {r.get("name"): r for r in bsrv.get("repos") or []}
                items = [f"{p} ({repo_reason(byname.get(p) or {}, now, repo_ext, panel_names)})" for p in probs]
                lst = ", ".join(items[:8]) + (f" и еще {n - 8}" if n > 8 else "")
                shown = f"{n} шт.: {lst}"
                fires.append(srv_fire(s, "backup_repo", rr, {"repos": shown}))
                fire_apply.append((s.id, "backup_repos", {"repos": probs, "ts": now.timestamp()}))
            elif not probs and prev_active:
                recoveries.append(_server_alert_text(
                    "backup_repo", s.name, "все репозитории снова в норме",
                    srv_url(s, "backup_repo"), recovery=True, group=s.group_name or ""))
                rec_apply.append((s.id, "backup_repos", []))

    if onboarding:
        # У серверов на подключении уходит только офлайн и критичный диск. Отметки придержанных
        # событий пишутся сразу, как будто сообщение дошло: после закрытия старое не придет
        marks.append((None, len(fires), len(fire_apply), len(recoveries), len(rec_apply)))
        now_apply: list[tuple[int, str, object]] = []
        nf, nfa, nr, nra = [], [], [], []
        for (sid, f0, a0, r0, ra0), (_n, f1, a1, r1, ra1) in zip(marks, marks[1:]):
            if sid in onboarding:
                kf, kfa = onboard_mod.split(sid, fires[f0:f1], fire_apply[a0:a1], now_apply)
                kr, kra = onboard_mod.split(sid, recoveries[r0:r1], rec_apply[ra0:ra1], now_apply)
            else:
                kf, kfa, kr, kra = fires[f0:f1], fire_apply[a0:a1], recoveries[r0:r1], rec_apply[ra0:ra1]
            nf += kf
            nfa += kfa
            nr += kr
            nra += kra
        fires[:], fire_apply[:], recoveries[:], rec_apply[:] = nf, nfa, nr, nra
        for sid, key, value in now_apply:
            apply(sid, key, value)

    # parse_mode=HTML: имя сервера идёт ссылкой <a href> (как у сайтов). Контент экранирован.
    if await alerts.dispatch(cfg, can_send, fires, threshold, parse_mode="HTML",
                             session_factory=session_factory):
        for sid, key, value in fire_apply:
            apply(sid, key, value)
    if await alerts.dispatch(cfg, can_send, recoveries, threshold, parse_mode="HTML",
                             session_factory=session_factory):
        for sid, key, value in rec_apply:
            apply(sid, key, value)

    if changed_ids or known_upd or known_add or onboard_closed:
        async with session_factory() as session:
            for sid in changed_ids:
                row = await session.get(Server, sid)
                if row is not None:
                    row.alert_state = cur_by_id[sid]
            for sid, kk, at, new_k, what in known_upd:
                row = await session.get(Server, sid)
                cur = dict((row.known_issues if row is not None else None) or {})
                if not isinstance(cur.get(kk), dict) or cur[kk].get("at") != at:
                    continue  # отметку успели поменять руками - их версия главнее
                if new_k is None:
                    cur.pop(kk)
                    session.add(AuditLog(username="авто", action="known_" + what, target=row.name[:255], detail=kk))
                else:
                    cur[kk] = new_k
                row.known_issues = cur or None
            for sid, kk, ent in known_add:
                row = await session.get(Server, sid)
                if row is None:
                    continue
                cur = dict(row.known_issues or {})
                if kk not in cur:  # отметку успели поставить руками - их версия главнее
                    cur[kk] = ent
                    row.known_issues = cur
            for sid, made in onboard_closed.items():
                row = await session.get(Server, sid)
                if row is not None and row.onboard_done_at is None:
                    row.onboard_done_at, row.onboard_by = now, "авто"
                    session.add(AuditLog(username="авто", action="onboard_done", target=row.name[:255],
                                         detail=", ".join(made)))
            await session.commit()


async def maintenance_tick(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> None:
    """Сверка после окон обслуживания и уборка давно прошедших разовых окон (app/maint.py).
    Сверка через maint.VERIFY_AFTER после конца: к этому времени сборщик уже прошел по
    серверам без снуза, и alert_state говорит, что сломано на самом деле."""
    msgs: list[alerts.Msg] = []
    async with session_factory() as session:
        windows = list(await session.scalars(select(MaintenanceWindow)))
        if not windows:
            return
        cfg = await settings_store.get_alert_config(session, settings)
        muted = await settings_store.get_muted(session)
        tz = cfg.get("tz") or "Europe/Moscow"
        for w in windows:
            if maint.finished(w, now) and (now - _aware(w.ends_at)).total_seconds() > maint.KEEP_DONE:
                await session.delete(w)
                continue
            occ = maint.due_verify(w, now, tz)
            if occ is None:
                continue
            w.verified_end = occ[1]
            srvs = list(await session.scalars(select(Server).where(
                Server.id.in_(w.server_ids or []), Server.enabled.is_(True)))) if w.server_ids else []
            chks = list(await session.scalars(select(Check).where(
                Check.id.in_(w.check_ids or []), Check.enabled.is_(True)))) if w.check_ids else []
            bad: list[str] = []
            for s in sorted(srvs, key=lambda x: x.name):
                if not seen_online(s, now):
                    bad.append(f"{s.name} - не на связи")
                    continue
                st = s.alert_state or {}
                kinds = sorted(k for k, v in st.items() if k in settings_store.SERVER_ALERT_KINDS
                               and isinstance(v, int) and not isinstance(v, bool) and v > 0)
                if kinds:
                    bad.append(f"{s.name} - " + ", ".join(settings_store.SERVER_ALERT_KINDS[k][0] for k in kinds))
            for c in sorted(chks, key=lambda x: x.name):
                if c.last_status in ("down", "degraded"):
                    bad.append(f"{c.name} - " + ("недоступен" if c.last_status == "down" else "тормозит"))
            text = maint.verify_text(w.title, bad, len(srvs), len(chks))
            msgs.append(alerts.Msg(("⚠️ " if bad else "🚧 ") + html.escape(text), "servers", "", kind="maint"))
        await session.commit()
    if msgs and not muted and alerts.alerts_enabled(cfg):
        await alerts.dispatch(cfg, True, msgs, int(cfg.get("flood_threshold", 6)), parse_mode="HTML",
                              session_factory=session_factory)


# Команда живёт секунды: агент забирает её за 1-15с, helper отвечает в пределах 90с
# (столько же ждёт спул на ноде). Всё, что висит дольше, до ноды не доехало.
_CMD_STUCK_SECONDS = 10 * 60
# disk_fix run ждет helper до 15 минут (docker builder prune по большому кэшу)
_FIX_STUCK_SECONDS = 20 * 60


async def _expire_commands(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Закрывает команды, застрявшие в pending/running.

    Иначе они висят вечно: живьём три «обновить restic» простояли в running трое
    суток, и в панели это выглядело как «кнопка ничего не делает» — без единого
    следа причины. Теперь пользователь видит явный отказ и может повторить.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_CMD_STUCK_SECONDS)
    fix_cutoff = now - timedelta(seconds=_FIX_STUCK_SECONDS)
    stuck = (BackupCommand.status.in_(("pending", "running")),
             or_(and_(BackupCommand.action != "disk_fix", BackupCommand.created_at < cutoff),
                 and_(BackupCommand.action == "disk_fix", BackupCommand.created_at < fix_cutoff)))
    async with session_factory() as session:
        # поставленные человеком - в журнал ошибок панели: кнопка "ничего не сделала"
        mine = list(await session.scalars(select(BackupCommand).where(*stuck, BackupCommand.created_by != "")))
        res = await session.execute(
            update(BackupCommand)
            .where(*stuck)
            .values(status="error", ok=False, result="агент не ответил — команда не доехала")
        )
        if res.rowcount:
            log.info("закрыто зависших backup-команд: %d", res.rowcount)
        await session.commit()
    for c in mine:
        c.result = "агент не ответил - команда не доехала"
        await panel_errors.command_failed(session_factory, "backup", c, soon=False)


# --- авто-очистка диска (галочка сервера, по умолчанию выключена) ---
# Только безопасная часть каталога "Освободить": то, что пересоздается само или является
# мусором. Лог контейнера (содержимое пропадает) в авторежим не входит. Разрешено ли
# действие на ноде, по-прежнему решает /etc/kervax/fix.conf (fix.allow в разборе места).
_AUTOFIX_ACTIONS = ("journal", "rotated-logs", "apt-cache", "dnf-cache", "coredumps",
                    "crash-reports", "docker-dangling", "docker-build-cache")
_AUTOFIX_LABEL = {
    "journal": "журнал systemd", "rotated-logs": "старые ротированные логи",
    "apt-cache": "кэш пакетов apt", "dnf-cache": "кэш пакетов dnf/yum",
    "coredumps": "дампы памяти упавших программ", "crash-reports": "отчеты о падениях",
    "docker-dangling": "образы docker без тега", "docker-build-cache": "кэш сборки docker",
}
_AUTOFIX_MIN = 64 * 1024 ** 2   # меньше - не стоит запуска
_AUTOFIX_COOLDOWN = 6 * 3600    # одно и то же действие на ноде - не чаще
_AUTOFIX_AGENT = (2, 13)        # агент, который передает disk_fix в спул helper'а


def _ver_tuple(v) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v or "0").split("."))
    except ValueError:
        return (0,)


async def autofix_disks(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Авто-очистка: у серверов с галочкой, когда раздел дошел до порога предупреждения,
    ставит в очередь одно безопасное действие "Освободить" - самое крупное. Следующее -
    когда это отработает (одна команда на сервер за раз) и только если раздел все еще выше
    порога. Одно и то же действие повторяется не чаще раза в 6 часов, чтобы не долбить
    ноду, где место съедает что-то другое. Возвращает, сколько команд поставлено."""
    queued = 0
    async with session_factory() as session:
        servers = list(await session.scalars(
            select(Server).where(Server.enabled.is_(True), Server.disk_autofix.is_(True))))
        for s in servers:
            rep = s.last_report or {}
            if _ver_tuple(rep.get("agent_version")) < _AUTOFIX_AGENT:
                continue
            block = disk_usage_block(rep)
            allow = set(((block or {}).get("fix") or {}).get("allow") or [])
            if not block or not allow:
                continue
            thr = s.disk_warn_percent or s.disk_alert_percent or s.disk_crit_percent or 85
            hot = {f.get("mount") for f in block["fs"]
                   if isinstance(f, dict) and float(f.get("pct") or 0) >= thr}
            if not hot:
                continue
            busy = await session.scalar(
                select(BackupCommand.id).where(
                    BackupCommand.server_id == s.id, BackupCommand.action == "disk_fix",
                    BackupCommand.status.in_(("pending", "running"))).limit(1))
            if busy:
                continue
            st = dict(s.disk_autofix_state or {})
            cands = [
                i for i in block.get("items") or []
                if isinstance(i, dict) and i.get("level") == "safe"
                and i.get("id") in _AUTOFIX_ACTIONS and i.get("id") in allow
                and i.get("mount") in hot and float(i.get("free") or 0) >= _AUTOFIX_MIN
                and now.timestamp() - float(st.get(i["id"]) or 0) >= _AUTOFIX_COOLDOWN
            ]
            if not cands:
                continue
            it = max(cands, key=lambda i: float(i.get("free") or 0))
            session.add(BackupCommand(server_id=s.id, action="disk_fix", mode="run",
                                      payload={"name": it["id"], "container": ""}, origin="auto"))
            st[it["id"]] = now.timestamp()
            s.disk_autofix_state = st
            # audit.record коммитит сессию - команда и отметка уходят вместе с записью журнала
            await audit.record(session, "авто", "backup_disk_fix", f"{it['id']}:run:auto",
                               f"srv={s.id}")
            queued += 1
        await session.commit()
    return queued


# --- разбор раздела по прогнозу ---
# Helper разбирает место сам только от 75%. Прогноз же обещает заполнение через считанные дни
# и у раздела пониже: из "Что сломано" человек попадал на диск, где нечего освободить и не
# видно, что растет. Такой раздел панель просит helper разобрать (helper держит разбор сутки),
# и к клику по пункту разбор уже приехал с отчетом.
_ANALYZE_AGENT = (2, 24)     # агент передает disk_fix analyze с разделом
_ANALYZE_HELPER = (0, 6)     # diskusage-setup с разбором по запросу
_ANALYZE_EVERY = 12 * 3600   # просим заново раньше, чем helper отпустит раздел


def disk_analyze_ok(rep: dict) -> bool:
    """Умеет ли нода разобрать раздел по запросу панели (кнопка "Разобрать" и прогноз)."""
    hv = ((rep or {}).get("setup_versions") or {}).get("diskusage-setup")
    return (_ver_tuple((rep or {}).get("agent_version")) >= _ANALYZE_AGENT
            and _ver_tuple(hv) >= _ANALYZE_HELPER)


async def request_job_logs(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Логи упавших Job для алерта (app/job_logs.py): на новый упавший Job - команда logs его
    поду, на готовые команды - забрать ответ. Возвращает, сколько команд поставлено."""
    now_ts = now.timestamp()
    queued = 0
    async with session_factory() as session:
        for s in list(await session.scalars(select(Server).where(Server.enabled.is_(True)))):
            rep = s.last_report or {}
            if not (rep.get("kube") or {}).get("access") or _ver_tuple(rep.get("agent_version")) < (2, 26):
                continue
            seen = (s.alert_state or {}).get("kube_jobs_seen")
            if seen is None or "kube_job" in (s.alert_mutes or []):
                continue
            snoozed = _parse_iso((s.alert_snoozes or {}).get("kube_job") or "")
            if snoozed is not None and snoozed > now:
                continue
            fresh = [j for j in kube_failed_jobs(rep) if job_logs.job_key(j) not in set(seen)]
            for j in fresh[:job_logs._JOBS]:
                pod = job_logs.ask(s.id, rep, j, now_ts)
                if pod is None:
                    continue
                c = KubeCommand(server_id=s.id, ns=pod[0], kind="pod", name=pod[1], action="logs",
                                tail=job_logs._TAIL)
                session.add(c)
                await session.flush()
                job_logs.asked(s.id, j, c.id)
                queued += 1
        want = job_logs.waiting_cmds()
        if want:
            rows = await session.execute(select(KubeCommand.id, KubeCommand.status, KubeCommand.result).where(
                KubeCommand.id.in_(want), KubeCommand.status.in_(("done", "error"))))
            for cid, status, result in rows.all():
                job_logs.answered(cid, status == "done", result or "")
        await session.commit()
    job_logs.prune(now_ts)
    return queued


async def request_disk_analysis(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Ставит в очередь разбор раздела, который по прогнозу заполнится в пределах предупреждения,
    а разбора у него нет. По одной команде disk_fix на сервер за раз, раздел - не чаще раза в
    _ANALYZE_EVERY. Возвращает, сколько команд поставлено."""
    queued = 0
    async with session_factory() as session:
        for s in list(await session.scalars(select(Server).where(Server.enabled.is_(True)))):
            rep = s.last_report or {}
            if not disk_analyze_ok(rep) or not seen_online(s, now):
                continue
            done = {f.get("mount") for f in ((disk_usage_block(rep) or {}).get("fs") or [])
                    if isinstance(f, dict)}
            st = dict(s.disk_autofix_state or {})
            want = [i["mount"] for i in dfc.fresh_items(s.disk_forecast, now) or []
                    if i.get("mount") and i["mount"] not in done
                    and float(i["eta_h"]) <= _FORECAST_WARN_H
                    and now.timestamp() - float(st.get(f"analyze:{i['mount']}") or 0) >= _ANALYZE_EVERY]
            if not want:
                continue
            busy = await session.scalar(
                select(BackupCommand.id).where(
                    BackupCommand.server_id == s.id, BackupCommand.action == "disk_fix",
                    BackupCommand.status.in_(("pending", "running"))).limit(1))
            if busy:
                continue
            m = want[0]
            session.add(BackupCommand(server_id=s.id, action="disk_fix", mode="run",
                                      payload={"name": "analyze", "container": "", "mount": m},
                                      origin="auto"))
            st[f"analyze:{m}"] = now.timestamp()
            s.disk_autofix_state = st
            await audit.record(session, "авто", "backup_disk_fix", f"analyze:run:{m}"[:120],
                               f"srv={s.id}")
            queued += 1
        await session.commit()
    return queued


async def send_autofix_note(session_factory: async_sessionmaker[AsyncSession],
                            server_id: int, cmd_id: int) -> None:
    """Результат авто-очистки - в каналы алертов: что панель удалила сама и сколько
    освободилось (или почему не вышло). Без этого удаление от root прошло бы тихо."""
    settings = get_settings()
    async with session_factory() as session:
        s = await session.get(Server, server_id)
        c = await session.get(BackupCommand, cmd_id)
        if s is None or c is None:
            return
        cfg = await settings_store.get_alert_config(session, settings)
        if await settings_store.get_muted(session) or not alerts.alerts_enabled(cfg):
            return
        name, group, rep = s.name, s.group_name or "", s.last_report or {}
        action = str((c.payload or {}).get("name") or "")
        ok, result = bool(c.ok), c.result or ""
    if action == "analyze":
        return  # разбор раздела по прогнозу ничего не удаляет
    label = _AUTOFIX_LABEL.get(action, action or "?")
    if ok:
        try:
            freed = float(json.loads(result).get("bytes") or 0)
        except (ValueError, AttributeError):
            freed = 0.0
        block = disk_usage_block(rep) or {}
        worst = max((f for f in block.get("fs") or [] if isinstance(f, dict)),
                    key=lambda f: float(f.get("pct") or 0), default=None)
        where = f" (диск {worst.get('mount')} был {worst.get('pct')}%)" if worst else ""
        detail, icon = f"автоочистка диска: {label}, освобождено {_fmt_size(freed)}{where}", "🧹"
    else:
        detail, icon = f"автоочистка диска не удалась ({label}): {result[:200]}", "⚠️"
    base = settings.panel_url.rstrip("/")
    url = f"{base}/?server={server_id}&sec=diskfill" if base else ""
    msg = _server_alert_text("disk", name, detail, url, icon=icon, group=group)
    await alerts.dispatch(cfg, True, [msg], int(cfg.get("flood_threshold", 6)),
                          parse_mode="HTML", session_factory=session_factory)


# История алертов: сотня строк в день на весь парк, а вопрос "как часто это было" задают и про
# квартал назад. Полгода, отдельно от хранения метрик.
_ALERT_HISTORY_KEEP = retention_mod.ALERTS_KEEP


async def _prune(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with session_factory() as session:
        ret = await settings_store.get_retention(session, settings)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=ret["sample_days"])
        srv_cutoff = now - timedelta(days=ret["server_days"])
        await session.execute(delete(CheckSample).where(CheckSample.ts < cutoff))
        await session.execute(delete(LocationSample).where(LocationSample.ts < cutoff))
        await session.execute(delete(CheckIpSample).where(CheckIpSample.ts < cutoff))
        await session.execute(delete(ServerMetric).where(ServerMetric.ts < srv_cutoff))
        await session.execute(delete(WebErrorSample).where(WebErrorSample.ts < srv_cutoff))
        # Сами строки с 5xx - неделю: в них адреса клиентов и запросы целиком. Обнуляем
        # полосой в два дня, чтобы не переписывать каждый раз весь месяц истории.
        lines_keep = retention_mod.WEB_LINES_KEEP
        await session.execute(
            update(WebErrorSample)
            .where(WebErrorSample.ts < now - lines_keep,
                   WebErrorSample.ts > now - lines_keep - timedelta(days=2))
            .values(lines=None)
        )
        await session.execute(delete(OomEvent).where(OomEvent.ts < srv_cutoff))
        await session.execute(delete(LinkSample).where(LinkSample.ts < srv_cutoff))
        await session.execute(delete(ServerTraffic).where(ServerTraffic.day < (now - traffic_mod.KEEP).date()))
        await session.execute(delete(ServerUsage).where(
            ServerUsage.hour < int((now - usage_mod.KEEP).timestamp() // 3600)))
        # история алертов маленькая, а нужна надолго: "как часто это было за квартал"
        await session.execute(delete(AlertEvent).where(AlertEvent.ts < now - _ALERT_HISTORY_KEEP))
        # docker/kube-команды (с логами) держим коротко — неделя, не тайм-серия
        cmd_floor = now - retention_mod.COMMANDS_KEEP
        await session.execute(delete(DockerCommand).where(DockerCommand.created_at < cmd_floor))
        await session.execute(delete(KubeCommand).where(KubeCommand.created_at < cmd_floor))
        await session.execute(delete(BackupCommand).where(BackupCommand.created_at < cmd_floor))
        # ручные проверки нужны ровно на время ожидания ответа — итог уже в журнале
        await session.execute(
            delete(ProbeRequest).where(ProbeRequest.created_at < now - retention_mod.PROBES_KEEP)
        )
        # левые запросы к панели: алерту нужны последние 10 минут, окну - сутки
        await session.execute(
            delete(RequestStat).where(RequestStat.minute < now - retention_mod.REQUESTS_KEEP)
        )
        # журнал ошибок панели: месяц и не больше panel_errors.MAX_ROWS строк
        await panel_errors.prune(session, now)
        # разовые проверки доменов из мастера живут часы; неделя — с большим запасом
        await session.execute(
            delete(DomainProbe).where(DomainProbe.started_at < now - retention_mod.DOMAIN_PROBES_KEEP)
        )
        # закрытые инциденты старше ретеншена тоже чистим
        await session.execute(
            delete(CheckIncident).where(
                CheckIncident.ended_at.is_not(None), CheckIncident.ended_at < cutoff
            )
        )
        await session.commit()


async def _maybe_auto_backup(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    async with session_factory() as session:
        cfg = await settings_store.get_backup_config(session, settings)
        if cfg["interval_hours"] <= 0:
            return  # автобэкап выключен
        now = datetime.now(timezone.utc)
        last_raw = await settings_store.get_raw(session, settings_store.BACKUP_LAST_KEY)
        if last_raw:
            last = datetime.fromisoformat(last_raw)
            if (now - last).total_seconds() < cfg["interval_hours"] * 3600:
                return
        name = await backup.write_auto_backup(session, settings, cfg["keep"])
        await settings_store.set_raw(
            session, settings_store.BACKUP_LAST_KEY, now.isoformat()
        )
        log.info("автобэкап записан: %s", name)


# Сводка о непокрытом — раз в сутки. Не чаще: это не поломка, а работа, которую
# делают за один прогон плейбука; и не реже — иначе новая СУБД неделями стоит без
# инвентаря, а появление кластера замечают, когда с него уже что-то падает.
_UNCOVERED_PERIOD = 24 * 3600
# Сколько дыра должна продержаться, чтобы попасть в сводку. Мгновенный срез врёт:
# на перезапуске docker (обновление пакета, ребут демона) агент честно видит «докер
# есть, доступа нет», и сводка звала чинить read-only proxy, который на самом деле
# стоял и работал. Через минуту всё было на месте, а сообщение уже ушло — в панели
# при этом ничего, потому что она показывает СЕЙЧАС.
_UNCOVERED_SUSTAIN = 3600


def _uncov_since(state: dict, keys: set[str], now: datetime) -> dict:
    """Отметки «когда эту дыру увидели впервые»: старые сохраняем, ушедшие забываем."""
    old = state.get("uncov") or {}
    return {k: (old.get(k) or now.isoformat()) for k in keys}


async def _track_uncovered(session_factory, now: datetime) -> None:
    """Каждый тик: помнить, с какого момента у ноды не хватает покрытия.

    Пишем в alert_state только когда набор дыр изменился — появилась новая или
    закрылась старая. На спокойном парке это ноль записей в БД.
    """
    cur = current_setup_versions()
    async with session_factory() as session:
        changed = False
        for srv in await session.scalars(select(Server).where(Server.enabled.is_(True))):
            keys = {k for k, _ in gaps(srv.last_report or {}, cur, srv.alert_mutes)} if seen_online(srv, now) else set()
            state = dict(srv.alert_state or {})
            fresh = _uncov_since(state, keys, now)
            if fresh != (state.get("uncov") or {}):
                if fresh:
                    state["uncov"] = fresh
                else:
                    state.pop("uncov", None)
                srv.alert_state = state
                changed = True
        if changed:
            await session.commit()


async def _daily_uncovered(session_factory, settings: Settings) -> None:
    """Одно сообщение в сутки: где появилось новое, а покрытия под это нет.

    Панель узнаёт об этом сама (агент пересобирает состав ноды каждый отчёт), но
    до сих пор говорила об этом только пунктом в «Требует действий». Пока туда не
    заглядывают, свежий Postgres стоит без инвентаря баз, а докер без доступа —
    без контейнеров: мониторинг вроде есть, а половины его нет.

    Шлём то, что чинится ОДНИМ действием (прогон плейбука или kube-setup), и в
    конце даём готовую команду. Устаревшие версии helper'ов сюда не попадают:
    там всё работает, просто не последней версии.
    """
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        cfg = await settings_store.get_alert_config(session, settings)
        muted = await settings_store.get_muted(session)
        rule = (await settings_store.get_server_alert_rules(session)).get("uncovered") or {}
        if muted or not rule.get("enabled") or not alerts.alerts_enabled(cfg):
            return
        last = await settings_store.get_uncovered_sent(session)
        if last and now.timestamp() - last < _UNCOVERED_PERIOD:
            return
        servers = list(await session.scalars(select(Server).where(Server.enabled.is_(True))))
        threshold = int(cfg.get("flood_threshold", 6))

    cur = current_setup_versions()
    lines: list[str] = []
    hosts: list[str] = []
    for srv in sorted(servers, key=lambda x: x.name or ""):
        # Оффлайновую ноду не зовём чинить: её отчёт устарел, и первым делом её
        # надо поднять — об этом уже есть свой алерт.
        if not seen_online(srv, now) or not _rule_scope_ok(rule, srv):
            continue
        if srv.snooze_until is not None and _aware(srv.snooze_until) > now:
            continue
        if "uncovered" in set(srv.alert_mutes or []):
            continue
        # Только то, что держится: см. _UNCOVERED_SUSTAIN. Свежую дыру пропускаем —
        # она попадёт в следующую сводку, если и правда никуда не денется.
        since = (srv.alert_state or {}).get("uncov") or {}
        g = [
            (k, txt)
            for k, txt in gaps(srv.last_report or {}, cur, srv.alert_mutes)
            if k in since
            and (now - (_parse_iso(since[k]) or now)).total_seconds() >= _UNCOVERED_SUSTAIN
        ]
        if not g:
            continue
        lines.append("• {}: {}".format(
            html.escape(srv.name or "", quote=False),
            "; ".join(html.escape(txt, quote=False) for _, txt in g),
        ))
        hosts.append(srv.name or "")

    if not lines:
        return
    cmd = "ansible-playbook playbooks/kervax_helpers.yml -l '{}'".format(",".join(hosts))
    default = settings_store.SERVER_ALERT_KINDS["uncovered"][1]
    tpl = rule.get("text") or default
    try:
        body = tpl.format(list="\n".join(lines), cmd=html.escape(cmd, quote=False), n=len(hosts))
    except (KeyError, IndexError, ValueError):
        body = default.format(list="\n".join(lines), cmd=html.escape(cmd, quote=False), n=len(hosts))
    text = alerts.Msg(f"🧩 {body}", "servers", "", kind="uncovered")
    if await alerts.dispatch(cfg, True, [text], threshold, parse_mode="HTML",
                             session_factory=session_factory):
        async with session_factory() as session:
            await settings_store.set_uncovered_sent(session, now.timestamp())
            await session.commit()


async def collector_loop(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    if settings.scheduler_tick <= 0:
        log.info("планировщик мониторов выключен (scheduler_tick=0)")
        return
    # «Пульс» для хостового watchdog — ОТДЕЛЬНОЙ задачей на фиксированной каденции
    # (60с), а НЕ в конце тика. Иначе длинный тик (много мониторов, медленные/
    # падающие проверки) или рестарт контейнера ложно «морили» пульс, и сторож
    # кричал «панель зависла», хотя она просто занята. Реальные отказы (процесс
    # мёртв, цикл событий завис, БД недоступна) по-прежнему валят запись пульса.
    hb_task = asyncio.create_task(heartbeat.heartbeat_loop(session_factory, settings))
    # кнопки под алертами и команды боту: своя задача, long polling держит запрос до 25 с
    tg_task = asyncio.create_task(tg_bot.poll_loop(session_factory, settings))
    last_prune: datetime | None = None
    # Стадии изолированы друг от друга. Была одна try на весь тик, и NameError в
    # серверных алертах уносил с собой прунинг и автобэкап панели — при этом наружу
    # шла одна строка в лог, а мониторинг молча стоял (нашли только вручную).
    async def stage(name: str, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 — соседние стадии должны отработать
            log.exception("стадия планировщика «%s» упала", name)

    try:
        while True:
            # Привязка локальных мониторов к нодам — ДО проверок, а не после: иначе
            # свежая галочка «проверять локально» встречает первый же цикл без ноды,
            # и монитор минуту горит «проверять некому» с открытым инцидентом.
            await stage("привязка локальных проверок", rebind_local_probes(session_factory))
            try:
                n = await run_due_checks(session_factory, settings)
                if n:
                    log.info("исполнено мониторов: %d", n)
                m = await run_location_probes(session_factory, settings)
                if m:
                    log.info("проверок через локации: %d", m)
            except Exception:  # noqa: BLE001 — цикл не должен падать
                log.exception("ошибка планировщика мониторов")
            await stage("алерты локаций", evaluate_location_alerts(
                session_factory, settings, datetime.now(timezone.utc)))
            # прогноз - до алертов: они читают его из servers.disk_forecast
            await stage("прогноз дисков", dfc.update_disk_forecasts(
                session_factory, datetime.now(timezone.utc)))
            # лента изменений - до алертов: они берут из нее новые изменения доступа
            await stage("изменения на сервере", srv_changes.track(
                session_factory, datetime.now(timezone.utc)))
            # хронология - тоже до алертов: строка "перед этим" берет из нее последние события
            await stage("хронология серверов", timeline_mod.track(
                session_factory, datetime.now(timezone.utc)))
            # логи упавших Job - до алертов: алерт о Job ждет хвост лога его пода
            await stage("логи упавших Job", request_job_logs(
                session_factory, datetime.now(timezone.utc)))
            await stage("серверные алерты", evaluate_servers(
                session_factory, settings, datetime.now(timezone.utc)))
            await stage("окна обслуживания", maintenance_tick(
                session_factory, settings, datetime.now(timezone.utc)))
            await stage("авто-очистка дисков", autofix_disks(
                session_factory, datetime.now(timezone.utc)))
            await stage("разбор дисков по прогнозу", request_disk_analysis(
                session_factory, datetime.now(timezone.utc)))
            await stage("зависшие команды", _expire_commands(session_factory))
            await stage("левые запросы к панели", request_watch.check(
                session_factory, settings, datetime.now(timezone.utc)))
            # внешняя проверка портов идет фоном: закрытый файрволом порт отвечает только таймаутом
            await stage("что открыто в интернет", open_ports.start_scan(
                session_factory, datetime.now(timezone.utc)))
            # достукивается ли панель до серверов на связи: эталон для строки в "недоступен"
            await stage("эталон связи с серверами", liveness.start_baseline(
                session_factory, datetime.now(timezone.utc)))
            # история износа SSD для прогноза до 90% (раз в час)
            await stage("износ дисков", disk_wear.refresh(
                session_factory, datetime.now(timezone.utc), disk_health_block))
            # версии без поддержки: пакеты сторонних репозиториев из списков пакетов (раз в час)
            await stage("версии ПО", software_mod.refresh(session_factory, datetime.now(timezone.utc)))
            # базы уязвимостей раз в сутки, загрузка и разбор фоном
            await stage("базы уязвимостей", vulns_mod.start_refresh(
                session_factory, datetime.now(timezone.utc)))
            # Прунинг двигает границу ретеншена медленно — незачем каждый тик гонять
            # DELETE по 3 большим таблицам. Раз в prune_interval_seconds (по умолч. час).
            now = datetime.now(timezone.utc)
            if last_prune is None or (
                now - last_prune
            ).total_seconds() >= settings.prune_interval_seconds:
                await stage("прунинг", _prune(session_factory, settings))
                last_prune = now
            await stage("учёт непокрытого", _track_uncovered(
                session_factory, datetime.now(timezone.utc)))
            await stage("сводка непокрытого", _daily_uncovered(session_factory, settings))
            await stage("автобэкап", _maybe_auto_backup(session_factory, settings))
            await asyncio.sleep(settings.scheduler_tick)
    finally:
        hb_task.cancel()
        tg_task.cancel()
