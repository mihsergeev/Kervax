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
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlparse

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import alerts, audit, backup, backup_growth, checks as checks_exec, custom_backups, docker_exposure, flaky, heartbeat, settings_store
from app import disk_forecast as dfc
from app.setup_scripts import current_setup_versions, gaps
from app.config import Settings, get_settings
from app.models import (
    AgentPathProbe,
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
    ProbeRequest,
    Server,
    ServerMetric,
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
    "cpu_spin": "🌀", "backup_growth": "💾📈", "docker_sock": "🔓🐳",
    # ⏳ — срок ещё не вышел, есть время спланировать; 🔥 — доставка уже встала
    "kube_expiry": "⏳", "flux_down": "🔥☸️", "kube_pod": "☸️🔥", "web_5xx": "🌐🔥",
    # репозиторий чартов недоступен, а развернутое работает: предупреждение, а не авария
    "flux_stale": "⚠️☸️",
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
    "db_conn": "services",
    "kube_expiry": "kube", "flux_down": "kube", "flux_stale": "kube", "kube_pod": "kube", "web_5xx": "web",
    "clock": "clock", "disk_health": "diskhealth", "inode": "diskfill", "disk_forecast": "diskfill", "units": "units",
    "cpu_spin": "cpueat",
}


# Диск шлёт три уровня, и ведущая иконка должна их различать: предупреждение,
# проблема, критично. Для остальных типов берём иконку из _SRV_ICON как есть.
_DISK_ICON = {1: "⚠️", 2: "🔴", 3: "🚨"}


# Раздел панели, к которому относится алерт — по нему персональная рассылка
# понимает, кого он касается (у учётки может не быть, скажем, «Бэкапов»).
# Вкладка карточки кластера в разделе "Кубер", куда ведет ссылка алерта.
_KUBE_TAB = {"kube_expiry": "expiry", "flux_down": "flux", "flux_stale": "flux", "kube_pod": "pods"}

_ALERT_SECTION = {
    "docker_loop": "docker",
    "docker_down": "docker",
    "docker_sock": "docker",
    "queue": "services",
    "backup_repo": "backups",
    "backup_rotation": "backups",
    "backup_lock": "backups",
    "backup_unmonitored": "backups",
    "backup_check": "backups",
    "backup_prune": "backups",
    "kube_pod": "kuber",
}


def _server_alert_text(
    kind: str, name: str, detail: str, link_url: str = "",
    recovery: bool = False, icon: str = "", group: str = "",
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
        group, kind=kind, target=name, recovery=recovery,
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


def _apply_expiry(row: Check, info, now: datetime, pending: list) -> None:
    """Сохраняет сроки TLS/домена и ставит эскалационные напоминания (по одному
    на каждый порог из ssl_warn_days/domain_warn_days). Невалидный/истёкший TLS
    на https уже даёт down основной проверки — здесь только про сроки."""
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
        if thr and info.domain_days > max(thr):
            if row.domain_alerted_days is not None:
                pending.append((
                    "domain", row.name, "",
                    f"регистрация домена продлена, до истечения {info.domain_days} дн.",
                    None, row.id, None, "✅",
                ))
            row.domain_alerted_days = None
        bucket = _expiry_bucket(info.domain_days, thr, row.domain_alerted_days)
        if bucket is not None:
            txt = _expiry_text(
                "регистрация домена", info.domain_days, "регистрация домена истекла"
            )
            pending.append(
                ("domain", row.name, "", txt, None, row.id, ("domain_alerted_days", bucket),
                 _expiry_icon("domain", info.domain_days))
            )


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
        )
    )
    new_status = outcome.status
    if new_status == "up":
        row.consecutive_fails = 0
    elif not manual:
        row.consecutive_fails = (row.consecutive_fails or 0) + 1

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
                _apply_expiry(row, exp_map[check.id], now, pending)
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
        if ids:
            rows = await session.execute(
                select(
                    Check.id, Check.group_name, Check.alert_mutes,
                    Check.target, Check.type, Check.port, Check.snooze_until,
                ).where(Check.id.in_(ids))
            )
            for i, g, m, tgt, typ, prt, snz in rows.all():
                groups[i] = g
                mutes[i] = set(m or [])
                meta[i] = (tgt, typ, prt)
                if snz is not None and _aware(snz) > now:
                    snoozed.add(i)
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
        tgt, typ, prt = meta.get(check_id, ("", "http", 0))
        host = _display_host(tgt, typ, prt)
        site_url = _site_url(tgt, typ)  # ссылка «открыть сам сайт» (без query/токена)
        link_url = f"{base}/?check={check_id}" if base and check_id is not None else ""
        zone = ""
        if kind == "domain":
            # в шапке показываем САМО истекающее имя, а не поддомен одного из
            # мониторов; ссылку на сайт убираем — она вела бы не туда, куда текст
            zone = checks_exec._registrable_domain(tgt or name)
            host, site_url = zone, ""
            if zone in dom_at:
                dom_n[zone] = dom_n.get(zone, 1) + 1
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
        # в истории отбой лежит под тем же видом, что и сама беда: фильтр по виду
        # показывает историю целиком
        texts.append(alerts.Msg(text, "sites", grp or "", kind=_SITE_HISTORY_KIND.get(kind, rk or kind),
                                target=name, recovery=kind in _SITE_RECOVERY))
        commits.append((kind, inc_id, check_id, flag))

    # дописываем счётчик только там, где мониторов реально больше одного
    for zone, idx in dom_at.items():
        n = dom_n.get(zone, 1)
        if n > 1:
            old = texts[idx]
            texts[idx] = alerts.Msg(
                f"{old} · мониторов: {n}", old.section, old.group,
                kind=old.kind, target=old.target, recovery=old.recovery,
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
            await session.scalars(select(Location).where(Location.enabled.is_(True)))
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
            await session.scalars(select(Location).where(Location.enabled.is_(True)))
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
    "web_5xx": "ошибки 5xx",
    "disk_health": "диски",
    "inode": "inode",
    "disk_forecast": "прогноз заполнения",
    "units": "юниты systemd",
    "cpu_spin": "процессы в пустом цикле",
    "backup_growth": "прирост бэкапа",
    "docker_sock": "docker-сокет у внешнего прокси",
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
        restarts = int(p.get("restarts") or 0)
        tail = f", {restarts} рестарта" if restarts >= 5 else ""
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
    if key == "flux_stale":
        return "репозиторий чартов снова отвечает"
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


def _dump_problems(bk: dict) -> list[str]:
    """Движки, чей дамп настроен, но не снимается. Два признака поломки: файлов нет
    вовсе (ни один дамп не удался) либо дамп отстал от последнего успешного бэкапа
    (перестал сниматься). НАМЕРЕННЫЙ пропуск из-за места сюда НЕ входит — для него
    отдельный алерт с actionable-текстом (_dump_skipped). Возвращает метки «движок»
    (с контейнером, если баз одного типа несколько). Пусто, если всё свежо."""
    last_backup = bk.get("last_backup_ts") or 0
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
        # дампа. Без этой проверки панель ныла «файлов нет» в первые же секунды.
        if files == 0 or dts == 0:
            if enabled and last_backup and last_backup > enabled:
                out.append(label)  # бэкап был после включения, а дампа нет → падает
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


def _cron_dump_problems(rep: dict, now: datetime) -> list[str]:
    """Проблемные дамп-CronJob'ы: приостановлен / прогон упал / ПЕРЕСТАЛ ЗАПУСКАТЬСЯ (давно
    нет успеха). Панель их РАНЬШЕ только детектила — теперь мониторит (правило: любой
    настроенный бэкап должен алертить). Идущий прямо сейчас прогон (active) не трогаем.
    Порог несвежести — 2 интервала расписания (минимум 2ч): daily-дамп молчит сутки, но
    не пропустит «дамп не сделался 2 дня» — даже если CronJob вообще перестал стартовать
    (тогда last_schedule и last_success замирают вместе, и сравнение ls-ok эту дыру не ловит)."""
    out: list[str] = []
    nowts = now.timestamp()
    for cj in ((rep.get("kube") or {}).get("cronjobs") or []):
        if not _looks_dump_cron(cj):
            continue
        name = f"{cj.get('ns', '?')}/{cj.get('name', '?')}"
        if cj.get("suspend"):
            out.append(f"{name} (приостановлен)")
            continue
        if cj.get("active"):
            continue  # прогон идёт — рано паниковать
        ls = int(cj.get("last_schedule") or 0)
        ok = int(cj.get("last_success") or 0)
        stale_after = max(_cron_interval_seconds(cj.get("schedule", "")) * 2, 2 * 3600)
        if ok > 0 and nowts - ok > stale_after:
            out.append(f"{name} (нет успешного дампа {round((nowts - ok) / 86400, 1)} дн)")
        elif ls and (ok == 0 or ls - ok > _CRON_DUMP_SLACK_SECONDS):
            out.append(f"{name} (последний прогон не удался)")
    return sorted(out)


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
                g = old[0]
                hint = f", старая группа {g.get('host') or '?'}: {int(g.get('n') or 0)} снапшотов"
            elif rotation_alive(r, observed.get(name), now):
                hint = ", похоже на старую группу: менялись хост или пути"
            else:
                hint = ""
            out.append(f"{name or '?'} ({int(age)} дн. > {limit}{hint})")
    return sorted(out)


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


def backup_rotation_items(s: Server, now: datetime) -> list[str]:
    """Что сейчас сказал бы алерт backup_rotation: старье пережило политику или ротация ничего
    не удаляет. Для "Что сломано" на главной; заглушенные репозитории не в счет, как в алерте."""
    rep = s.last_report or {}
    bs = rep.get("backup_server") or {}
    if not bs.get("present"):
        return []
    ext = bsrv_extra(rep)
    arch = getattr(s, "backup_repo_archive", None) or {}
    over, _seen = rotation_overflow(bs, dict((s.alert_state or {}).get("rotation_over") or {}), now, ext, arch)
    muted = set(s.backup_repo_mutes or [])
    return [x for x in rotation_stale_repos(bs, now, ext, arch) + over if x.split(" (")[0] not in muted]


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


def disk_health_problems(block: dict, short: bool = False) -> list[tuple[int, str, str]]:
    """Поломки дисков: (уровень, ключ, текст), серьезные первыми. Ключ стабилен между
    отчетами: по нему алерт понимает, что сломалось НОВОЕ (второй диск при уже
    развалившемся RAID), а не то, о чем уже сказано. Склеенные в одну строку массивы
    дают по записи на массив без текста и одну запись с текстом без ключа: досинхронизи-
    ровавшийся первым md42 не должен выглядеть новой поломкой md43. Ошибки журнала ядра
    сюда не входят, у них своя выдержка (см. _server_conditions). short - диск только по
    имени, без модели и серийника: для сводки на главной."""
    out: list[tuple[int, str, str]] = []
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
        for lvl, _key, txt in disk_health_problems(hb, short=True):
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
            if not isinstance(u, dict) or not u.get("unit") or u["unit"] in muted:
                continue
            if ref - float(u.get("since") or 0) < _UNIT_MIN_AGE:
                continue
            u_since = datetime.fromtimestamp(float(u["since"]), timezone.utc).isoformat() if u.get("since") else None
            add("units", unit_level(u), f"{u['unit']}: {unit_why(u)}", "units", f"unit:{u['unit']}", u_since)
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
            "", "db_conn", state.get("db_conn_since"), section="services")
    # Ошибки 5xx: окно алерта (15 минут), без часа после него, который держит алерт одной историей
    w5 = web_5xx_now(s, web_err, now)
    if w5 is not None and w5[0] and w5[2]:
        add("web_5xx", 2, f"ошибки 5xx: {w5[1] / w5[2] * 100:.1f}% запросов за 15 минут", "web", "web_5xx")
    # Очереди RabbitMQ выше своего порога, как у алерта queue
    hot = []
    for svc in rep.get("services") or []:
        if not isinstance(svc, dict) or svc.get("kind") != "rabbitmq":
            continue
        for q in svc.get("queues") or []:
            thr = queue_threshold(s, queue_key(svc.get("source") or "", q))
            if thr > 0 and queue_depth(q) >= thr:
                hot.append(f"{q.get('name') or '?'} ({queue_depth(q)})")
    if hot:
        add("queue", 2, "переполнены очереди RabbitMQ: " + ", ".join(hot[:3])
            + (f" и еще {len(hot) - 3}" if len(hot) > 3 else ""), "", "queue", section="services")
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
        add("backup_dump", 2, "дамп СУБД не обновляется: " + ", ".join(broken), "", "backup_dump",
            state.get("backup_dump_since"), section="backups", srv=False)
    if skipped:
        add("backup_dump_space", 2, f"дамп пропущен, мало места (свободно {free}%): " + ", ".join(skipped),
            "", "backup_dump_space", section="backups", srv=False)
    if (rep.get("kube") or {}).get("access"):
        cron = _cron_dump_problems(rep, now)
        if cron:
            add("backup_cron", 2, "дамп-CronJob не отрабатывает: " + "; ".join(cron[:3])
                + (f" и еще {len(cron) - 3}" if len(cron) > 3 else ""), "", "backup_cron",
                section="backups", srv=False)
    # Бэкап-сервер: не прошла проверка целостности репозитория
    bsrv = rep.get("backup_server") or {}
    if bsrv.get("present"):
        bad = failed_checks(bsrv, bsrv_extra(rep), set(getattr(s, "backup_repo_mutes", None) or []), now)
        if bad:
            add("backup_check", 2, "не прошла проверка целостности: " + ", ".join(bad[:3])
                + (f" и еще {len(bad) - 3}" if len(bad) > 3 else ""), "", "backup_check",
                section="backups", srv=True)
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
               "backup_missing", "dns")


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


async def web_error_window(session: AsyncSession,
                           now: datetime) -> dict[int, tuple[float, float, int, dict, float]]:
    """{server_id: (запросов, ошибок 5xx, минут с данными, {ключ лога: {минута: ошибок}},
    ошибок на пробах сканеров)} за последние 15 минут. Два запроса на тик, а не по запросу
    на ноду. Разбивка по логам - чтобы вычесть заглушенные и посчитать минуты с ошибками
    без них. Минуты, где ошибки только на пробах (probe_only), в разбивку не попадают, их
    ошибки идут отдельной суммой: ее вычитают из общего числа так же, как заглушенные."""
    since = now - _WEB_ERR_WINDOW
    rows = await session.execute(
        select(ServerMetric.server_id, func.sum(ServerMetric.web_rpm),
               func.sum(ServerMetric.web_5xx), func.count())
        .where(ServerMetric.ts >= since, ServerMetric.web_5xx.is_not(None))
        .group_by(ServerMetric.server_id)
    )
    # Минуты с ошибками - по минутам helper'а (src_ts), а не по точкам истории: одна и та
    # же минута helper'а может попасть в две точки подряд.
    per: dict[int, dict[str, dict[int, int]]] = {}
    probes: dict[int, dict[tuple[str, int], int]] = {}
    for sid, label, log, src, e5, paths in await session.execute(
        select(WebErrorSample.server_id, WebErrorSample.label, WebErrorSample.log,
               WebErrorSample.src_ts, WebErrorSample.e5, WebErrorSample.paths)
        .where(WebErrorSample.ts >= since, WebErrorSample.e5 > 0)
    ):
        key = web_label_key(label or log)
        if probe_only(paths):
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
    by_log: dict[str, list] = {}
    codes: dict[str, int] = {}
    paths: dict[str, int] = {}
    hosts: dict[str, int] = {}
    for r in sorted(rows, key=lambda x: x.ts):
        if web_label_key(r.label or r.log) in muted:
            continue  # заглушенный лог: про него не пишем, даже если он и шумит
        if probe_only(r.paths):
            continue  # минута, где 500 только сканерам: в алерт она не входит
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
    logs = sorted(by_log.values(), key=lambda x: -x[1])
    return {"logs": logs, "codes": codes, "paths": paths, "hosts": hosts}


def web_where_text(where: dict) -> str:
    """"Больше всего: nginx (anketa.shop.example, mobile.shop.example +2) - 41. Коды: 502 - 30, 504 - 11. Чаще всего
    падает: /api/v1/anketa - 38.» Каждый кусок - только если данные есть: коды и пути
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
                url,
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
    # ответы сканерам на /.env, /@fs/ и подобное: сайт от них не сломан
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

    def sustain(key: str, breach: bool, ctx: dict) -> None:
        """Пороговую метрику алертим лишь если превышение ДЕРЖИТСЯ ≥ sustain_s секунд
        (гасим кратковременные спайки). Момент начала «жарки» пишем в ctx["since"]
        (persist каждый тик); None = вернулось в норму / оффлайн → сброс."""
        if breach and online:
            since = state.get(f"{key}_since") or now.isoformat()
            started = _parse_iso(since)
            held = (now - started).total_seconds() if started else 0.0
            lvl = 1 if held >= sustain_s else 0
        else:
            since, lvl = None, 0
        ctx["since"] = since
        out[key] = (lvl, ctx)

    rep = s.last_report or {}
    cpu = rep.get("cpu_percent")
    if s.cpu_alert_percent and cpu is not None:
        # cause заполняется в момент отправки (нужна история метрик), но в ctx он
        # обязан быть всегда: без него шаблон с {cause} не отрендерится
        sustain("cpu", cpu >= s.cpu_alert_percent,
                {"value": round(cpu), "threshold": s.cpu_alert_percent, "cause": ""})
    if s.mem_alert_percent and rep.get("mem_total"):
        memp = rep.get("mem_used", 0) / rep["mem_total"] * 100
        sustain("mem", memp >= s.mem_alert_percent,
                {"value": round(memp), "threshold": s.mem_alert_percent, "cause": ""})
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
            sustain("flux_stale", True, fx[1])
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
                {"pods": bad_pods_text(bad_pods), "n": len(bad_pods)})

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
        probs = disk_health_problems(hb)
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
        bad = [u for u in ub.get("units") or []
               if isinstance(u, dict) and u.get("unit") and u["unit"] not in muted_u
               and ref - float(u.get("since") or 0) >= _UNIT_MIN_AGE]
        u_lvl = max((unit_level(u) for u in bad), default=0)
        out["units"] = (u_lvl, {
            "detail": units_text(bad, ref) if bad else "", "level": u_lvl,
            # какие юниты лежат: новый упавший при том же уровне - повод для сообщения
            "sig": sorted(u["unit"] for u in bad),
        })

    # Одинаковые процессы, которые давно крутят по ядру каждый (агент 2.21). На k8s-a-prc 47
    # зависших chrome жгли 45 ядер из 96, а CPU-алерт молчал: до порога не доходило. Агент
    # старше 2.21 групп не шлет - ключа нет, как у юнитов без helper'а.
    if online and "cpu_groups" in rep:
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
    # Свои бэкапы ноды (настроены без панели; находит helper): упал прогон, давно не было
    # успешного, выключен таймер. Состояния устойчивые, как и у restic, — без дебаунса.
    if online:
        own = custom_backups.problems(s, now)
        out["backup_custom"] = (1 if own else 0, {"jobs": "; ".join(own[:6]), "n": len(own)})
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
    can_send = alerts.alerts_enabled(cfg) and not muted
    base = settings.panel_url.rstrip("/")
    threshold = int(cfg.get("flood_threshold", 6))

    def srv_url(s: Server, key: str = "") -> str:
        """Ссылка на деталь сервера. Для типовых алертов добавляем &sec=<раздел>,
        чтобы клик открывал СРАЗУ нужный дашборд (OOM/RAM → «Память», CPU → «CPU» и т.д.).
        Docker-алерты ведут на раздел «Докер» с открытой карточкой хоста (?docker=id)."""
        if not (base and s.id is not None):
            return ""
        if key.startswith("docker"):
            return f"{base}/?docker={s.id}"
        if key == "queue":
            # ведём СРАЗУ в очереди этой ноды: из алерта человек идёт смотреть,
            # что там накопилось, а не в общий список сервисов
            return f"{base}/?services={s.id}&queues=1"
        if key in _KUBE_TAB:
            # Раздел "Кубер" с открытым кластером и сразу на нужной вкладке: сроки, Flux,
            # поды. В карточке сервера кластера нет, и ссылка вела мимо.
            return f"{base}/?kube={s.id}&ktab={_KUBE_TAB[key]}"
        if key in ("backup_repo", "backup_rotation", "backup_lock", "backup_unmonitored", "backup_check",
                   "backup_prune"):
            return f"{base}/?backupsrv={s.id}"
        if key.startswith("backup"):
            return f"{base}/?backup={s.id}"
        sec = _SRV_SECTION.get(key)
        return f"{base}/?server={s.id}" + (f"&sec={sec}" if sec else "")

    base_cache: dict[int, dict[str, float]] = {}
    # имена подов по uid - один раз за тик: воркер без доступа к kube-api пишет поды по uid
    pod_names = pod_uid_names(servers)
    # имена нод панели - для сверки с клиентами бэкап-серверов (один раз за тик)
    panel_names = panel_server_names(servers)
    # какие резолверы тормозят у каждой ноды прямо сейчас: в алерте DNS сказать, что то же у
    # других нод с теми же резолверами (сбой у провайдера, а не на ноде)
    dns_slow_by: dict[int, set[str]] = {}
    for _s in servers:
        _rep = _s.last_report or {}
        if seen_online(_s, now) and dns_problem(_rep):
            dns_slow_by[_s.id] = {x["addr"] for x in dns_slow_servers(dns_block(_rep) or {})}

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
            if key == "kube_expiry" and ctx.get("expired"):
                ico = "⛔"  # срок не «скоро», а уже вышел — это поломка, а не напоминание
            return _server_alert_text(
                key, s.name, _fmt_rule(default, s, ctx), url, icon=ico,
                group=s.group_name or "",
            )
        txt = html.escape(_fmt_rule(rule["text"], s, ctx))
        return alerts.Msg(
            txt + (f"\n🔗 {html.escape(url)}" if url else ""),
            _ALERT_SECTION.get(key, "servers"),
            s.group_name or "", kind=key, target=s.name,
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

    def apply(sid: int, key: str, value: object) -> None:
        cur_by_id[sid][key] = value
        changed_ids.add(sid)

    for s in servers:
        st = cur_by_id[s.id]
        # снуз: сервер временно приглушён — алерты не шлём (но состояние не трогаем,
        # чтобы после снуза оставшаяся проблема снова сработала)
        if s.snooze_until is not None and _aware(s.snooze_until) > now:
            continue
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
        # состояние дебаунса ведём КАЖДЫЙ тик, даже без смены уровня алерта — иначе
        # оно не накопится (при level==prev цикл ниже делает continue без записи).
        # «<тип>_since» — момент начала «жарки» (по времени); throttle — «_streak».
        for k, (_lvl, cx) in conds.items():
            if "since" in cx and st.get(f"{k}_since") != cx["since"]:
                apply(s.id, f"{k}_since", cx["since"])
            if "streak" in cx and int(st.get(f"{k}_streak", 0)) != cx["streak"]:
                apply(s.id, f"{k}_streak", cx["streak"])
            # с какого момента подряд тормозит каждый резолвер DNS (для "Что сломано")
            if "res" in cx and (st.get(f"{k}_res") or {}) != cx["res"]:
                apply(s.id, f"{k}_res", cx["res"] or None)
            # с какого момента проблема видна - для "Что сломано" ("sda изношен - 2 дня");
            # ведем для любого вида и до проверки мьютов: заглушенное тоже остается проблемой
            frm = st.get(f"{k}_from")
            if _lvl > 0 and not frm:
                apply(s.id, f"{k}_from", now.isoformat())
            elif _lvl <= 0 and frm:
                apply(s.id, f"{k}_from", None)
        for key, (level, ctx) in conds.items():
            rule = rules.get(key) or _fallback_rule(key)
            if not rule["enabled"] or not _rule_scope_ok(rule, s):
                continue  # правило выключено или сервер вне области применения
            if _muted(key, level, mutes):
                continue  # тип (или его нижние уровни) заглушён для этого сервера
            prev = int(st.get(key, 0))
            if key in ("disk_health", "units"):
                # Тот же уровень, но сломалось НОВОЕ (второй диск при уже развалившемся
                # RAID, второй упавший юнит): молчать нельзя, хотя уровень не вырос. Что
                # сломано - ключи в sig.
                sk = f"{key}_sig"
                old_sig = set(st.get(sk) or [])
                new_sig = set(ctx.get("sig") or [])
                if level == prev:
                    if level and new_sig - old_sig:
                        fires.append(srv_fire(s, key, rule, ctx))
                        fire_apply.append((s.id, sk, sorted(new_sig)))
                    elif new_sig != old_sig:
                        apply(s.id, sk, sorted(new_sig))
                    continue
                if level > prev:
                    fire_apply.append((s.id, sk, sorted(new_sig)))
                elif level:
                    apply(s.id, sk, sorted(new_sig))
                else:
                    rec_apply.append((s.id, sk, []))
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
                fires.append(srv_fire(s, key, rule, ctx))
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
                    _server_alert_text(key, s.name, detail, srv_url(s, key), recovery=True)
                )
                rec_apply.append((s.id, key, 0))
                if key == "offline":
                    rec_apply.append((s.id, "offline_said", 0))
            else:  # де-эскалация (crit→problem): без алерта, просто снижаем уровень
                apply(s.id, key, level)
        # перезагрузка (аптайм сброшен) — одноразовый алерт на новый rebooted_at
        rb = rules.get("reboot")
        if s.rebooted_at is not None and "reboot" not in mutes and rb and rb["enabled"] and _rule_scope_ok(rb, s):
            stamp = _aware(s.rebooted_at).isoformat()
            if st.get("reboot_at") != stamp:
                fires.append(srv_fire(s, "reboot", rb, {}))
                fire_apply.append((s.id, "reboot_at", stamp))
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
                            srv_url(s, "docker_loop"), recovery=True))
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
                        fires.append(srv_fire(s, "docker_down", r_down,
                                              {"container": name, "state": state, "policy": policy or "no",
                                               "why": why}))
                        cs["alerted_down"] = True
                else:
                    cs["down_since"] = None
                    if was_down:  # снова поднялся; отметку снимаем и у заглушенного
                        if down_ok:
                            # не поднялся, но это уже не авария: остановили руками, сняли policy
                            back = (f"контейнер {name} снова работает" if state == "running"
                                    else f"контейнер {name} остановлен штатно, тревога снята")
                            recoveries.append(_server_alert_text(
                                "docker_down", s.name, back, srv_url(s, "docker_down"), recovery=True))
                        cs["alerted_down"] = False
                dstate[name] = cs
            # контейнеры, пропавшие из списка (удалены/пересозданы) — чистим состояние
            for gone in [n for n in dstate if n not in seen_names]:
                dstate.pop(gone, None)
            if dstate != (st.get("docker") or {}):
                apply(s.id, "docker", dstate)

        # Очереди RabbitMQ: алертим те, что переросли свой порог. Состояние — на
        # КАЖДУЮ очередь: очередей десятки, и одна разбухшая не должна глушить
        # остальные, а вернувшаяся в норму обязана дать отбой сама по себе.
        qr = rules.get("queue")
        if online_now and qr and qr["enabled"] and "queue" not in mutes and _rule_scope_ok(qr, s):
            qstate = dict(st.get("queues") or {})
            seen_q: set[str] = set()
            for svc in (s.last_report or {}).get("services") or []:
                if svc.get("kind") != "rabbitmq":
                    continue
                src = svc.get("source") or ""
                for q in svc.get("queues") or []:
                    key = queue_key(src, q)
                    seen_q.add(key)
                    thr = queue_threshold(s, key)
                    if thr <= 0:  # порога нет — очередь не сторожим
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
                            srv_url(s, "queue"), recovery=True))
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
            stale = [x for x in rotation_stale_repos(bsrv_rot, now, rot_ext, rot_arch) + over
                     if x.split(" (")[0] not in set(s.backup_repo_mutes or [])]
            was_stale = bool(st.get("rotation_stale"))
            if stale and not was_stale:
                shown = ", ".join(stale[:6]) + (f" и ещё {len(stale) - 6}" if len(stale) > 6 else "")
                fires.append(srv_fire(s, "backup_rotation", rot_r, {"repos": shown}))
                fire_apply.append((s.id, "rotation_stale", 1))
            elif not stale and was_stale:
                recoveries.append(_server_alert_text(
                    "backup_rotation", s.name, "ротация снова вычищает старые снапшоты",
                    srv_url(s, "backup_rotation"), recovery=True))
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
                    srv_url(s, "backup_lock"), recovery=True))
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
                    srv_url(s, "backup_check"), recovery=True))
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
                    srv_url(s, "backup_prune"), recovery=True))
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
                    srv_url(s, "backup_repo"), recovery=True))
                rec_apply.append((s.id, "backup_repos", []))

    # parse_mode=HTML: имя сервера идёт ссылкой <a href> (как у сайтов). Контент экранирован.
    if await alerts.dispatch(cfg, can_send, fires, threshold, parse_mode="HTML",
                             session_factory=session_factory):
        for sid, key, value in fire_apply:
            apply(sid, key, value)
    if await alerts.dispatch(cfg, can_send, recoveries, threshold, parse_mode="HTML",
                             session_factory=session_factory):
        for sid, key, value in rec_apply:
            apply(sid, key, value)

    if changed_ids:
        async with session_factory() as session:
            for sid in changed_ids:
                row = await session.get(Server, sid)
                if row is not None:
                    row.alert_state = cur_by_id[sid]
            await session.commit()


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
    async with session_factory() as session:
        res = await session.execute(
            update(BackupCommand)
            .where(BackupCommand.status.in_(("pending", "running")),
                   or_(and_(BackupCommand.action != "disk_fix", BackupCommand.created_at < cutoff),
                       and_(BackupCommand.action == "disk_fix", BackupCommand.created_at < fix_cutoff)))
            .values(status="error", ok=False, result="агент не ответил — команда не доехала")
        )
        if res.rowcount:
            log.info("закрыто зависших backup-команд: %d", res.rowcount)
        await session.commit()


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
_ALERT_HISTORY_KEEP = timedelta(days=180)


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
        await session.execute(
            update(WebErrorSample)
            .where(WebErrorSample.ts < now - timedelta(days=7),
                   WebErrorSample.ts > now - timedelta(days=9))
            .values(lines=None)
        )
        await session.execute(delete(OomEvent).where(OomEvent.ts < srv_cutoff))
        # история алертов маленькая, а нужна надолго: "как часто это было за квартал"
        await session.execute(delete(AlertEvent).where(AlertEvent.ts < now - _ALERT_HISTORY_KEEP))
        # docker/kube-команды (с логами) держим коротко — неделя, не тайм-серия
        await session.execute(
            delete(DockerCommand).where(DockerCommand.created_at < now - timedelta(days=7))
        )
        await session.execute(
            delete(KubeCommand).where(KubeCommand.created_at < now - timedelta(days=7))
        )
        await session.execute(
            delete(BackupCommand).where(BackupCommand.created_at < now - timedelta(days=7))
        )
        # ручные проверки нужны ровно на время ожидания ответа — итог уже в журнале
        await session.execute(
            delete(ProbeRequest).where(ProbeRequest.created_at < now - timedelta(days=1))
        )
        # разовые проверки доменов из мастера живут часы; неделя — с большим запасом
        await session.execute(
            delete(DomainProbe).where(DomainProbe.started_at < now - timedelta(days=7))
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
            keys = {k for k, _ in gaps(srv.last_report or {}, cur)} if seen_online(srv, now) else set()
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
            for k, txt in gaps(srv.last_report or {}, cur)
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
            await stage("серверные алерты", evaluate_servers(
                session_factory, settings, datetime.now(timezone.utc)))
            await stage("авто-очистка дисков", autofix_disks(
                session_factory, datetime.now(timezone.utc)))
            await stage("разбор дисков по прогнозу", request_disk_analysis(
                session_factory, datetime.now(timezone.utc)))
            await stage("зависшие команды", _expire_commands(session_factory))
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
