"""Фоновый планировщик мониторов: гоняет «созревшие» проверки, пишет тайм-серии,
ведёт инциденты (up→down переходы) и шлёт пороговые алерты.

Тик каждые settings.scheduler_tick секунд. Прунит старые снимки по retention.
"""

import asyncio
import html
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import urlparse

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import alerts, backup, checks as checks_exec, heartbeat, settings_store
from app.config import Settings
from app.models import (
    AgentProbe,
    Check,
    CheckIncident,
    CheckIpSample,
    CheckSample,
    BackupCommand,
    DockerCommand,
    KubeCommand,
    Location,
    LocationResult,
    LocationSample,
    OomEvent,
    Server,
    ServerMetric,
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


_ALERT_ICON = {"recovery": "✅", "locrec": "✅", "ssl": "🔐", "domain": "🌐", "locpart": "🌍"}


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
    "queue": "🐇", "backup_rotation": "🧹",
    "backup_missing": "💾", "backup_failed": "💾", "backup_stale": "💾", "backup_repo": "💾",
    "backup_dump": "💾", "backup_dump_space": "🈵", "backup_cron": "💾", "clock": "🕐",
    # ⏳ — срок ещё не вышел, есть время спланировать; 🔥 — доставка уже встала
    "kube_expiry": "⏳", "flux_down": "🔥☸️",
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
    "kube_expiry": "kube", "flux_down": "kube",
    "clock": "clock",
}


# Диск шлёт три уровня, и ведущая иконка должна их различать: предупреждение,
# проблема, критично. Для остальных типов берём иконку из _SRV_ICON как есть.
_DISK_ICON = {1: "⚠️", 2: "🔴", 3: "🚨"}


# Раздел панели, к которому относится алерт — по нему персональная рассылка
# понимает, кого он касается (у учётки может не быть, скажем, «Бэкапов»).
_ALERT_SECTION = {
    "docker_loop": "docker",
    "docker_down": "docker",
    "queue": "services",
    "backup_repo": "backups",
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
        group,
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
    local = [c for c in due if c.probe_server_id]
    remote = [c for c in due if not c.probe_server_id]
    outcomes_map: dict[int, object] = {}
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
        for c in local:
            outcomes_map[c.id] = checks_exec.outcome_from_agent(
                c, probes.get(c.id), now, c.degraded_ms
            )
    outcomes = [outcomes_map.get(c.id) for c in due]
    # «медленные» сроки (TLS/домен) обновляем только у созревших для этого мониторов
    refresh = [c for c in due if _needs_expiry(c, now, settings)]
    exp_results = await _gather_capped(
        [checks_exec.probe_expiry(c) for c in refresh], cap, jitter
    )
    exp_map: dict[int, checks_exec.ExpiryInfo] = {
        c.id: r
        for c, r in zip(refresh, exp_results)
        if isinstance(r, checks_exec.ExpiryInfo)
    }

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
            msg = outcome.message[:512]
            session.add(
                CheckSample(
                    check_id=check.id,
                    status=outcome.status,
                    latency_ms=outcome.latency_ms,
                    value=outcome.value,
                    message=msg,
                    ts=now,
                )
            )
            row = await session.get(Check, check.id)
            if row is None:
                continue

            new_status = outcome.status
            row.consecutive_fails = 0 if new_status == "up" else row.consecutive_fails + 1

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
                if not open_inc.notified and row.consecutive_fails >= threshold:
                    pending.append(
                        ("bad", row.name, new_status, msg, open_inc.id, row.id, None, "")
                    )
            elif open_inc is not None:
                open_inc.ended_at = now
                if open_inc.notified:
                    # несём up-сообщение («HTTP 200 · N мс» / «порт открыт …») —
                    # чтобы в восстановлении был виден код/латентность, а не пусто
                    pending.append(("recovery", row.name, "up", msg, None, row.id, None, ""))

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
            if outcome.ip_results is not None:
                row.last_ip_results = outcome.ip_results
                for ipr in outcome.ip_results:
                    session.add(CheckIpSample(
                        check_id=row.id, ip=ipr["ip"], status=ipr["status"],
                        latency_ms=ipr.get("latency_ms"), ts=now,
                    ))

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
}


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
        texts.append(alerts.Msg(text, "sites", grp or ""))
        commits.append((kind, inc_id, check_id, flag))

    # дописываем счётчик только там, где мониторов реально больше одного
    for zone, idx in dom_at.items():
        n = dom_n.get(zone, 1)
        if n > 1:
            old = texts[idx]
            texts[idx] = alerts.Msg(
                f"{old} · мониторов: {n}", old.section, old.group
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
    "backup_cron": "дамп-CronJob", "clock": "время",
    "kube_expiry": "сроки Kubernetes", "flux_down": "доставка Flux",
}

# Единицы пороговых метрик. Нужны отбою: голое «снова в норме» не отвечает на
# первый вопрос инженера — сколько освободилось. Показываем «диск снова в норме:
# 82% (было 90%)», где «было» — значение на момент срабатывания.
_SRV_UNIT = {"cpu": "%", "mem": "%", "disk": "%", "conntrack": "%", "db_conn": "%",
             "temp": "°C", "disktemp": "°C"}


def _recovery_detail(key: str, st: dict, ctx: dict) -> str:
    """Текст отбоя порогового алерта. Прежнее значение берём из alert_state
    («<тип>_val», кладётся вместе с самим срабатыванием); если его нет — алерт
    объявлен ещё старой версией, тогда показываем хотя бы текущее."""
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

# Чем грозит и что делать — отдельно для «ещё не истёк» и «уже истёк». Без этого
# алерт сообщает дату и молчит о главном; на «ИСТЁК 49 дн. назад» первый вопрос
# инженера — «а почему тогда всё работает?», и ответ должен быть в самом сообщении.
_KUBE_ADVICE = {
    ("cluster-cert", False): "после этой даты компоненты control-plane перестанут принимать друг друга — перевыпустите заранее",
    ("cluster-cert", True): "control-plane обычно уже не поднимется после рестарта — перевыпускайте немедленно",
    ("kubeconfig", False): "после этой даты этим файлом в кластер не зайти — перевыпустите",
    ("kubeconfig", True): "этим файлом в кластер уже не зайти — перевыпустите",
    ("kubelet-cert", False): "он должен ротироваться сам; если дата близко — ротация не работает, и нода выпадет из кластера",
    ("kubelet-cert", True): "ротация не сработала — нода отвалится от кластера, чините kubelet",
    ("flux-token", False): "после этой даты Flux перестанет забирать изменения из Git — выпустите новый токен и обновите секрет",
    ("flux-token", True): "Flux уже не может забирать изменения — выпустите новый токен и обновите секрет",
    ("secret-cert", False): "обновите, иначе TLS у того, кто им пользуется, перестанет работать",
    ("secret-cert", True): "кластер жив — значит секрет сейчас никто не перечитывает, но при рестарте пода сломается то, что им пользуется; обновите или удалите, если он больше не нужен",
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

# «[Job/op-dg-prod/op-dg-migrations status: 'Failed']» — так Flux называет объект,
# который не поднялся. Человеку нужнее «Job op-dg-migrations: Failed».
_FLUX_OBJ = re.compile(r"\[(\w+)/[^/\]]+/([^\s\]]+) status: '([^']+)'\]")


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
        eng = d.get("engine") or "?"
        cont = d.get("container") or ""
        label = f"{eng}@{cont}" if cont else eng
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
        eng = d.get("engine") or "?"
        cont = d.get("container") or ""
        labels.append(f"{eng}@{cont}" if cont else eng)
        fp = d.get("skip_free_pct")
        if isinstance(fp, int) and 0 <= fp < min_free:
            min_free = fp
    return sorted(labels), (min_free if labels else 0)


def _lock_stuck(r: dict, now_ts: float) -> bool:
    """Лок висячий (а не от идущего прямо сейчас бэкапа)?"""
    if not r.get("locked"):
        return False
    ts = r.get("lock_ts") or 0
    # старый helper (< v5) времени лока не отдаёт — трактуем как раньше, иначе
    # на неапгрейженных нодах висячие локи перестали бы замечаться вовсе
    if ts <= 0:
        return True
    return (now_ts - ts) > _BACKUP_LOCK_STUCK_SECONDS


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


def _backup_problem_repos(bs: dict, muted: set, now: datetime) -> list[str]:
    """Имена НЕ заглушённых репо бэкап-сервера, требующих внимания: битые (нет config),
    с висячим локом или устаревшие (давно не принимали бэкап). Отсортировано."""
    out = []
    for r in bs.get("repos") or []:
        name = r.get("name") or ""
        if not name or name in muted:
            continue
        last = r.get("last_activity") or 0
        stale = last > 0 and (now.timestamp() - last) > _BACKUP_REPO_STALE_SECONDS
        if (not r.get("valid")) or _lock_stuck(r, now.timestamp()) or stale:
            out.append(name)
    return sorted(out)


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


def rotation_stale_repos(bsrv: dict, now: datetime) -> list[str]:
    """Репозитории, где старейший снапшот пережил собственную политику хранения.

    Главный признак мёртвой ротации, инвариантный к причине: неважно, сломалась ли
    группировка forget, упал prune или снят cron — старьё просто перестаёт исчезать.
    Именно этого сигнала не хватало, когда 17 дней всё было зелёным."""
    out: list[str] = []
    for r in bsrv.get("repos") or []:
        oldest = int(r.get("oldest_snapshot") or 0)
        limit = rotation_max_age_days(r)
        if oldest <= 0 or limit <= 0:
            continue  # нет метрики (старый helper) или нет политики — молчим
        age = (now.timestamp() - oldest) / 86400
        if age > limit:
            out.append(f"{r.get('name') or '?'} ({int(age)} дн. > {limit})")
    return sorted(out)


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


def _server_conditions(s: Server, now: datetime) -> dict[str, tuple[int, dict]]:
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
        sustain("cpu", cpu >= s.cpu_alert_percent,
                {"value": round(cpu), "threshold": s.cpu_alert_percent})
    if s.mem_alert_percent and rep.get("mem_total"):
        memp = rep.get("mem_used", 0) / rep["mem_total"] * 100
        sustain("mem", memp >= s.mem_alert_percent,
                {"value": round(memp), "threshold": s.mem_alert_percent})
    if rep.get("disks"):
        worst = max(
            (d["used"] / d["total"] * 100 for d in rep["disks"] if d.get("total")),
            default=0.0,
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
                             "severity": sev, "level": lvl})
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
        worst = None
        for db in rep.get("db_stats") or []:
            limit = db.get("conn_max") or 0
            if limit <= 0:  # движок не отдал лимит — считать процент не из чего
                continue
            pct = (db.get("conn_used") or 0) / limit * 100
            if worst is None or pct > worst[0]:
                worst = (pct, db)
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
    if s.kube_expiry_alert_days:
        soon, near = None, 0
        for it in rep.get("kube_expiry") or []:
            exp = it.get("expires") or 0
            if exp <= 0:
                continue
            days = (exp - now.timestamp()) / 86400
            if days > s.kube_expiry_alert_days:
                continue
            near += 1
            if soon is None or exp < soon[0]:
                soon = (exp, days, it)
        if soon is not None:
            exp, days, it = soon
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
            sustain("kube_expiry", True, {
                "value": phrase,
                "what": _KUBE_KIND.get(kind, "срок"),
                "where": where,
                "advice": _KUBE_ADVICE.get((kind, days < 0), ""),
                "date": datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%d.%m.%Y"),
                "more": f" + ещё {near - 1} на этом сервере" if near > 1 else "",
            })
        else:
            sustain("kube_expiry", False, {})

    # Flux уже сломан. Второй половиной той же беды: срок можно проспать, а токен —
    # ещё и отозвать руками, и тогда предупреждать не о чем, доставка встала сразу.
    # Читаем Ready у ресурсов Flux; «в процессе» не считаем поломкой, а дебаунс
    # (те же 15 минут по умолчанию) гасит обычную реконсиляцию.
    broken = [f for f in (rep.get("flux") or [])
              if not f.get("ready") and (f.get("reason") or "") not in _FLUX_TRANSIENT]
    if rep.get("flux") is not None:
        if broken:
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
            tail = []
            if len(pool) > 1:
                tail.append(f"ещё {len(pool) - 1} сломано")
            if waiting:
                tail.append(f"{waiting} ждут этого")
            sustain("flux_down", True, {
                "what": f.get("kind") or "ресурс Flux",
                "where": where,
                # человеческая причина, а сырой reason — в скобках: по нему гуглят
                "reason": f"{why} ({reason})" if why else reason,
                "message": _flux_detail(f.get("message") or "") or "без пояснения",
                "hint": ("\n↳ " + hint.format(ns=ns or "namespace", name=name or "имя"))
                        if hint else "",
                "more": f" [{', '.join(tail)}]" if tail else "",
            })
        else:
            sustain("flux_down", False, {})

    if s.disk_temp_alert_c:  # макс по устройствам с датчиком (на VM датчика обычно нет)
        temps = [d["temp"] for d in (rep.get("disk_devs") or []) if d.get("temp") is not None]
        if temps:
            hottest = max(temps)
            sustain("disktemp", hottest >= s.disk_temp_alert_c,
                    {"value": round(hottest), "threshold": s.disk_temp_alert_c})

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
        ok = bool(bk.get("configured")) or bool(getattr(s, "backup_not_required", False))
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
            broken = _dump_problems(bk)
            # через sustain: сломанный дамп — состояние на дни, оно удержание переживёт,
            # а одиночный тик на стыке циклов (см. _BACKUP_DUMP_LAG_SECONDS) — нет
            sustain("backup_dump", bool(broken),
                    {"engines": ", ".join(broken), "n": len(broken)})
            skipped, free = _dump_skipped(bk)
            out["backup_dump_space"] = (1 if skipped else 0,
                                        {"engines": ", ".join(skipped), "free": free})
    # Дамп-CronJob'ы в кластере: мониторим прогоны НЕЗАВИСИМО от restic-бэкапа (kube-нода
    # часто без него). Панель раньше только детектила «дамп настроен» — теперь алертит,
    # если CronJob приостановлен или его последний прогон не завершился успехом.
    if online and (rep.get("kube") or {}).get("access"):
        cron = _cron_dump_problems(rep, now)
        out["backup_cron"] = (1 if cron else 0, {"jobs": "; ".join(cron[:6]), "n": len(cron)})
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
        if key in ("backup_repo", "backup_rotation"):
            return f"{base}/?backupsrv={s.id}"
        if key.startswith("backup"):
            return f"{base}/?backup={s.id}"
        sec = _SRV_SECTION.get(key)
        return f"{base}/?server={s.id}" + (f"&sec={sec}" if sec else "")

    def srv_fire(s: Server, key: str, rule: dict, ctx: dict) -> alerts.Msg:
        """Текст срабатывания: дефолт → богатый формат (иконка + имя-ссылкой + суть),
        кастомный шаблон пользователя → рендерим как есть + строка со ссылкой."""
        url = srv_url(s, key)
        default = settings_store.SERVER_ALERT_KINDS[key][1]
        if rule["text"] == default or rule["text"] in settings_store.LEGACY_SERVER_DEFAULTS:
            # у диска три уровня — ведущая иконка должна их различать
            ico = _DISK_ICON.get(int(ctx.get("level") or 0), "") if key == "disk" else ""
            return _server_alert_text(
                key, s.name, _fmt_rule(default, s, ctx), url, icon=ico,
                group=s.group_name or "",
            )
        txt = html.escape(_fmt_rule(rule["text"], s, ctx))
        return alerts.Msg(
            txt + (f"\n🔗 {html.escape(url)}" if url else ""),
            _ALERT_SECTION.get(key, "servers"),
            s.group_name or "",
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
        conds = _server_conditions(s, now)
        # состояние дебаунса ведём КАЖДЫЙ тик, даже без смены уровня алерта — иначе
        # оно не накопится (при level==prev цикл ниже делает continue без записи).
        # «<тип>_since» — момент начала «жарки» (по времени); throttle — «_streak».
        for k, (_lvl, cx) in conds.items():
            if "since" in cx and st.get(f"{k}_since") != cx["since"]:
                apply(s.id, f"{k}_since", cx["since"])
            if "streak" in cx and int(st.get(f"{k}_streak", 0)) != cx["streak"]:
                apply(s.id, f"{k}_streak", cx["streak"])
        for key, (level, ctx) in conds.items():
            rule = rules.get(key) or _fallback_rule(key)
            if not rule["enabled"] or not _rule_scope_ok(rule, s):
                continue  # правило выключено или сервер вне области применения
            if _muted(key, level, mutes):
                continue  # тип (или его нижние уровни) заглушён для этого сервера
            prev = int(st.get(key, 0))
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
                elif was_loop and not loops and loop_ok:  # рестарты прекратились
                    recoveries.append(_server_alert_text(
                        "docker_loop", s.name, f"контейнер {name}: перезапуски прекратились",
                        srv_url(s, "docker_loop"), recovery=True))
                    cs["alerted_loop"] = False
                # «упал»: не running при наличии restart-policy, держится ≥ sustain
                is_down = state in _DOCKER_DOWN_STATES and policy in _DOCKER_RESTART_POLICIES
                was_down = bool(cs.get("alerted_down"))
                down_ok = r_down and r_down["enabled"] and "docker_down" not in mutes and _rule_scope_ok(r_down, s)
                if is_down:
                    since = cs.get("down_since") or now.isoformat()
                    cs["down_since"] = since
                    started = _parse_iso(since)
                    held = (now - started).total_seconds() if started else 0.0
                    if held >= sustain_s and not was_down and down_ok:
                        fires.append(srv_fire(s, "docker_down", r_down,
                                              {"container": name, "state": state, "policy": policy or "no"}))
                        cs["alerted_down"] = True
                else:
                    cs["down_since"] = None
                    if was_down and down_ok:  # снова поднялся
                        recoveries.append(_server_alert_text(
                            "docker_down", s.name, f"контейнер {name} снова работает",
                            srv_url(s, "docker_down"), recovery=True))
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
            stale = [x for x in rotation_stale_repos(bsrv_rot, now)
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

        # Бэкап-сервер: репозитории, требующие внимания (устарели/битые/залочены), кроме
        # заглушённых. Алертим при появлении НОВЫХ проблемных, recovery — когда все чисты.
        bsrv = (s.last_report or {}).get("backup_server") or {}
        rr = rules.get("backup_repo")
        if online_now and bsrv.get("present") and "backup_repo" not in mutes:
            probs = _backup_problem_repos(bsrv, set(s.backup_repo_mutes or []), now)
            prev = st.get("backup_repos")
            if isinstance(prev, list):  # старый формат (голый список) → мигрируем
                prev = {"repos": prev, "ts": 0}
            prev = prev if isinstance(prev, dict) else {}
            prev_active = bool(prev.get("repos"))
            prev_ts = float(prev.get("ts") or 0)
            repo_ok = rr and rr["enabled"] and _rule_scope_ok(rr, s)
            # НЕ срочный: шлём при ПОЯВЛЕНИИ проблемы, потом напоминаем не чаще раза в сутки —
            # а НЕ на каждое изменение набора (набор флапает по свежести/локам → был спам).
            due = not prev_active or (now.timestamp() - prev_ts >= _BACKUP_REPO_REALERT)
            if probs and repo_ok and due:
                n = len(probs)
                lst = ", ".join(probs[:8]) + (f" и ещё {n - 8}" if n > 8 else "")
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


async def _expire_commands(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Закрывает команды, застрявшие в pending/running.

    Иначе они висят вечно: живьём три «обновить restic» простояли в running трое
    суток, и в панели это выглядело как «кнопка ничего не делает» — без единого
    следа причины. Теперь пользователь видит явный отказ и может повторить.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_CMD_STUCK_SECONDS)
    async with session_factory() as session:
        res = await session.execute(
            update(BackupCommand)
            .where(BackupCommand.status.in_(("pending", "running")),
                   BackupCommand.created_at < cutoff)
            .values(status="error", ok=False, result="агент не ответил — команда не доехала")
        )
        if res.rowcount:
            log.info("закрыто зависших backup-команд: %d", res.rowcount)
        await session.commit()


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
        await session.execute(delete(OomEvent).where(OomEvent.ts < srv_cutoff))
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
            await stage("серверные алерты", evaluate_servers(
                session_factory, settings, datetime.now(timezone.utc)))
            await stage("зависшие команды", _expire_commands(session_factory))
            # Прунинг двигает границу ретеншена медленно — незачем каждый тик гонять
            # DELETE по 3 большим таблицам. Раз в prune_interval_seconds (по умолч. час).
            now = datetime.now(timezone.utc)
            if last_prune is None or (
                now - last_prune
            ).total_seconds() >= settings.prune_interval_seconds:
                await stage("прунинг", _prune(session_factory, settings))
                last_prune = now
            await stage("автобэкап", _maybe_auto_backup(session_factory, settings))
            await asyncio.sleep(settings.scheduler_tick)
    finally:
        hb_task.cancel()
