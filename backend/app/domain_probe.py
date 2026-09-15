"""Разовая проверка доменов, найденных на серверах, — перед постановкой на мониторинг.

Мастер «Домены, найденные на серверах» предлагал заводить мониторы вслепую: откроется
ли сайт панели, человек узнавал уже от красного монитора. Сайт за белым списком снаружи
не виден вовсе, его надо проверять изнутри сервера, а мастер об этом молчал.

Теперь каждый предложенный домен проверяется дважды:
  - снаружи — панелью, ровно как его будет проверять обычный монитор;
  - изнутри — агентом той ноды, где домен найден. Задание идёт тем же путём, что
    «Проверить сейчас» у локального монитора (см. manual_probe), только без монитора.
Мастер показывает оба итога и предлагает рабочий вариант.

Это не мониторинг: проверка идёт при открытии мастера, итог хранится FRESH_SECONDS и
при повторном открытии показывается как есть. Перепроверить можно кнопкой.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import checks as checks_exec
from app import manual_probe
from app.collector import _aware, seen_online
from app.models import DomainProbe, ProbeRequest, Server
from app.schemas import DomainProbeOut

# Сколько живёт итог: повторное открытие мастера показывает его, а не гоняет по сайтам
# новые запросы.
FRESH_SECONDS = 12 * 3600
# «Перепроверить» жмут дважды подряд — второй раз проверку не перезапускаем.
RECHECK_MIN_SECONDS = 20
# Сколько сайтов панель проверяет одновременно: сотня мёртвых доменов с таймаутом по
# 10 секунд не должна тянуться минутами, но и лавину запросов разом устраивать незачем.
CONCURRENCY = 16
# Проверка снаружи, не закончившаяся за это время, прервалась (перезапуск панели).
EXT_STALE_SECONDS = 600
# Строки старше этого вычищает ретеншен.
KEEP_DAYS = 7


def target(domain: str) -> str:
    """Адрес проверки — тот же, что получит монитор из мастера (checks.adopt_domains)."""
    return f"https://{domain}"


def _local_deadline() -> int:
    return manual_probe.deadline_seconds(manual_probe.domain_check(""))


def _busy(row: DomainProbe, now: datetime) -> bool:
    """Проверка ещё идёт: ответ снаружи или изнутри не пришёл и не просрочен."""
    age = (now - _aware(row.started_at)).total_seconds()
    if row.ext_ts is None and age < EXT_STALE_SECONDS:
        return True
    return bool(row.local_request_id and not row.local_status and age <= _local_deadline())


async def start(
    session: AsyncSession,
    domains: list[str],
    serving: dict[str, tuple[int, str]],
    servers: dict[int, Server],
    by_user: str,
    force: bool,
    now: datetime,
) -> list[str]:
    """Запускает проверку доменов, у которых нет свежего итога (force — и со свежим).

    Проверку изнутри поручает агенту сразу; домены, которые надо проверить снаружи,
    возвращает — это делает фоновая задача run_external. Не коммитит.

    domains — уже отобранные вызывающим: годные в монитор и найденные на его серверах;
    serving — «домен → (id, имя) ноды, которая его обслуживает»."""
    if not domains:
        return []
    rows = {
        r.domain: r
        for r in await session.scalars(select(DomainProbe).where(DomainProbe.domain.in_(domains)))
    }
    started: list[str] = []
    for domain in domains:
        row = rows.get(domain)
        if row is not None:
            age = (now - _aware(row.started_at)).total_seconds()
            if _busy(row, now) or age < RECHECK_MIN_SECONDS:
                continue
            if not force and age < FRESH_SECONDS:
                continue
        else:
            row = DomainProbe(domain=domain)
            session.add(row)
        row.started_at = now
        row.by_user = by_user
        row.ext_ts, row.ext_status, row.ext_latency_ms, row.ext_message = None, "", None, ""
        row.local_ts, row.local_status, row.local_latency_ms, row.local_message = None, "", None, ""
        row.local_request_id = None
        sid, name = serving[domain]
        row.local_server_id = sid
        srv = servers.get(sid)
        if srv is not None and seen_online(srv, now):
            req = ProbeRequest(check_id=0, server_id=sid, url=target(domain), created_at=now)
            session.add(req)
            await session.flush()  # нужен номер запроса: по нему придёт ответ агента
            row.local_request_id = req.id
            srv.probe_pending_at = now
        else:
            row.local_ts = now
            row.local_status = "none"
            row.local_message = f"сервер {name} не на связи — изнутри проверить некому"
        started.append(domain)
    return started


def _same_moment(a: datetime, b: datetime) -> bool:
    return abs((_aware(a) - _aware(b)).total_seconds()) < 0.001


async def run_external(
    session_factory: async_sessionmaker[AsyncSession], domains: list[str], started_at: datetime
) -> None:
    """Проверка снаружи — фоном, итог каждого домена пишется, как только готов: мастер
    показывает их по мере поступления, а не ждёт самый медленный сайт."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(domain: str) -> None:
        async with sem:
            try:
                outcome = await checks_exec.run_check(manual_probe.domain_check(target(domain)))
            except Exception as exc:  # noqa: BLE001 — один домен не должен ронять остальные
                outcome = checks_exec.CheckOutcome(
                    "down", message=checks_exec.humanize_error(str(exc) or type(exc).__name__)
                )
        async with session_factory() as session:
            row = await session.get(DomainProbe, domain)
            # домен успели перепроверить — этот итог уже никому не нужен
            if row is None or not _same_moment(row.started_at, started_at):
                return
            row.ext_ts = datetime.now(timezone.utc)
            row.ext_status = outcome.status
            row.ext_latency_ms = outcome.latency_ms
            row.ext_message = outcome.message[:512]
            await session.commit()

    await asyncio.gather(*(one(d) for d in domains))


def _kind(status: str, message: str) -> tuple[str, int]:
    """Что не так — одним словом, для значка в строке мастера: (вид, HTTP-код).

    Полный текст причины уходит отдельно (подсказка на значке), но строк в мастере
    бывает сотня, и «сервер не ответил на попытку соединения (ConnectTimeout)» в каждой
    читать некому. Вид — код, текст к нему живёт в словаре интерфейса."""
    m = re.search(r"HTTP (\d{3})", message)
    code = int(m.group(1)) if m else 0
    if status != "down":
        return "", code
    low = message.lower()
    if code:
        return "http", code
    if any(s in low for s in ("белый список", "оборвал", "разорвал", "reset", "eof")):
        return "reset", 0
    if any(s in low for s in ("tls", "ssl", "x509", "сертификат")):
        return "tls", 0
    if any(s in low for s in ("dns", "name or service", "nodename", "ip-адрес")):
        return "dns", 0
    if any(s in low for s in ("refused", "закрыт", "отклонил")):
        return "refused", 0
    if any(s in low for s in ("timeout", "не ответил", "время ожидания", "вовремя")):
        return "timeout", 0
    if any(s in low for s in ("подключиться", "маршрут", "сеть недоступна", "unreachable")):
        return "connect", 0
    return "other", 0


def out(row: DomainProbe, now: datetime, names: dict[int, str]) -> DomainProbeOut:
    """Строка для мастера. Просроченное ожидание дочитываем здесь: ответ агента мог не
    прийти вовсе, а фоновую проверку снаружи мог оборвать перезапуск панели."""
    age = (now - _aware(row.started_at)).total_seconds()
    if row.ext_ts is not None:
        ext = (row.ext_status, row.ext_latency_ms, row.ext_message)
    elif age >= EXT_STALE_SECONDS:
        ext = ("down", None, "проверка не завершилась — перепроверьте")
    else:
        ext = ("pending", None, "")
    ext_kind, ext_code = _kind(ext[0], ext[2])

    server = names.get(row.local_server_id or 0, "")
    local_kind = ""
    if row.local_status:
        local = (row.local_status, row.local_latency_ms, row.local_message)
        if row.local_status == "none":
            local_kind = "offline"
    elif row.local_request_id and age <= _local_deadline():
        local = ("pending", None, "")
    else:
        local = (
            "none", None,
            f"агент на сервере {server} не прислал ответ за {_local_deadline()} с — "
            "изнутри проверить не удалось",
        )
        local_kind = "no_answer"
    if not local_kind:
        local_kind, local_code = _kind(local[0], local[2])
    else:
        local_code = 0

    return DomainProbeOut(
        domain=row.domain, started_at=_aware(row.started_at),
        ext_status=ext[0], ext_latency_ms=ext[1], ext_message=ext[2],
        ext_kind=ext_kind, ext_code=ext_code,
        local_status=local[0], local_latency_ms=local[1], local_message=local[2],
        local_kind=local_kind, local_code=local_code, local_server=server,
    )


async def rows_out(
    session: AsyncSession, domains: list[str], names: dict[int, str], now: datetime
) -> list[DomainProbeOut]:
    """Свежие итоги по этим доменам (старше FRESH_SECONDS — как будто проверки не было)."""
    if not domains:
        return []
    rows = await session.scalars(
        select(DomainProbe).where(
            DomainProbe.domain.in_(domains),
            DomainProbe.started_at >= now - timedelta(seconds=FRESH_SECONDS),
        )
    )
    return [out(r, now, names) for r in sorted(rows, key=lambda r: r.domain)]
