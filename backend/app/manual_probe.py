"""«Проверить сейчас» для сайта, который проверяет агент на ноде.

Сайт за белым списком панель проверить не может, поэтому планово его проверяет агент
изнутри сервера. Ручная проверка раньше всё равно шла из панели — туда, куда монитор
как раз не ходит: панель, стоящая в белом списке, получала «работает», агент изнутри
продолжал видеть сбой, и через минуту монитор снова краснел. Человек жал кнопку
двадцать раз подряд и не мог понять, работает сайт или нет.

Теперь кнопка просит у агента свежую проверку. Задание уходит под отрицательным
номером (-id запроса): агент такого номера ещё не видел и проверяет сразу, а не по
своему расписанию, и ответ под этим номером гарантированно снят после нажатия —
плановый результат агент повторяет в каждом отчёте, и отличить свежий от старого
по нему нельзя. Агент 2.7+ забирает задание быстрым опросом команд (раз в секунду) и
отвечает сразу; более старый — следующим отчётом, а на время ожидания панель просит
его отчитываться чаще.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import checks as checks_exec
from app.collector import _aware, record_outcome
from app.models import AgentProbe, Check, DomainProbe, ProbeRequest, Server

# С этой версии агент забирает ручную проверку опросом команд и отвечает сразу.
FAST_AGENT = (2, 7)
# Интервал отчёта, пока у старого агента в пути ручная проверка: ответ уезжает только
# со следующим отчётом, и обычные 15 секунд ожидания на кнопке — слишком долго.
PENDING_REPORT_INTERVAL = 2
# Сколько живёт пометка «у ноды есть ручные проверки» (servers.probe_pending_at).
_PENDING_FLAG_TTL = 180


def domain_check(url: str) -> SimpleNamespace:
    """Монитор, которого ещё нет: проверка домена из мастера «найденные домены».

    Настройки — как у монитора, который мастер заведёт (значения по умолчанию), иначе
    мастер обещал бы «работает» про сайт, который сам монитор сочтёт упавшим. Повторов
    нет: это разовый взгляд перед добавлением, а не вердикт для алерта."""
    return SimpleNamespace(
        id=0, type="http", target=url, method="GET", timeout_ms=10000,
        interval_seconds=60, degraded_ms=2000, retries=0, expected_status="200-399",
        keyword_up="", keyword_down="", http_headers="", auth_method="", auth_user="",
        auth_pass="", ignore_tls=False, check_all_ips=False, probe_server_id=None,
    )


def _ver(v: object) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v or "").strip().split("."))
    except ValueError:
        return (0,)


def fast_agent(server: Server | None) -> bool:
    return server is not None and _ver(server.agent_version) >= FAST_AGENT


def _timeout_s(check: Check) -> float:
    return min(max(check.timeout_ms or 10000, 1000), 60000) / 1000


def deadline_seconds(check: Check) -> int:
    """Сколько ждать ответа агента, прежде чем сказать «проверить не удалось».

    Старый агент заберёт задание следующим отчётом (до 15 с), проверка займёт до
    таймаута монитора, ответ уедет ещё одним отчётом — и запас на сеть."""
    return int(30 + _timeout_s(check))


def authority_seconds(check: Check) -> int:
    """Сколько ручной результат главнее планового (AgentProbe.manual_until).

    Плановую проверку агент делает раз в интервал монитора (не чаще 15 с) при очередном
    отчёте и отдаёт следующим — за это время свежий плановый ответ гарантированно
    приходит, и дальше верим уже ему."""
    return int(max(check.interval_seconds or 60, 15) + 30 + _timeout_s(check))


def site_probe_task(check, task_id: int) -> dict:
    """Задание агенту: что проверить изнутри сервера.

    Агент ходит ТОЛЬКО на localhost (адрес он подменяет сам), поэтому URL здесь — это
    не «куда пойти», а «чьим именем представиться»: Host и SNI. Панель не может послать
    агента ни на какой другой хост, и заставить его сканировать сеть тоже не может —
    даже будучи захваченной."""
    return {
        "id": task_id,
        "url": check.target,
        "method": check.method or "GET",
        "timeout_ms": check.timeout_ms,
        "interval": check.interval_seconds,
        "expected_status": check.expected_status,
        "keyword_up": check.keyword_up,
        "keyword_down": check.keyword_down,
        "headers": check.http_headers or "",
        "auth_user": check.auth_user if check.auth_method == "basic" else "",
        "auth_pass": check.auth_pass if check.auth_method == "basic" else "",
        "ignore_tls": bool(check.ignore_tls),
    }


async def _open(session: AsyncSession, server: Server, now: datetime) -> list[tuple]:
    """Ручные проверки ноды, ещё ждущие ответа и не просроченные: (запрос, монитор).

    У проверки домена из мастера монитора нет — вместо него настройки по умолчанию."""
    at = server.probe_pending_at
    if at is None or (now - _aware(at)).total_seconds() > _PENDING_FLAG_TTL:
        return []
    rows = (
        await session.execute(
            select(ProbeRequest, Check)
            .outerjoin(Check, Check.id == ProbeRequest.check_id)
            .where(
                ProbeRequest.server_id == server.id,
                ProbeRequest.ts.is_(None),
                ProbeRequest.created_at >= now - timedelta(seconds=_PENDING_FLAG_TTL),
            )
            .order_by(ProbeRequest.id)
        )
    ).all()
    out = []
    for req, chk in rows:
        if req.check_id == 0 and req.url:
            chk = domain_check(req.url)
        if chk is None:
            continue  # монитор удалили, пока запрос ждал агента
        if (now - _aware(req.created_at)).total_seconds() <= deadline_seconds(chk):
            out.append((req, chk))
    return out


async def take_fast_tasks(session: AsyncSession, server: Server, now: datetime) -> list[dict]:
    """Задания для быстрого опроса команд. Только агенту, который умеет их исполнять:
    старый проигнорировал бы поле, а задание считалось бы забранным."""
    if not fast_agent(server):
        return []
    out = []
    for req, chk in await _open(session, server, now):
        if req.taken_at is not None:
            continue
        req.taken_at = now
        out.append(site_probe_task(chk, -req.id))
    return out


async def tasks_for_report(session: AsyncSession, server: Server, now: datetime) -> list[dict]:
    """Задания в ответ на отчёт: не забранные быстрым опросом (старый агент) либо
    забранные, но оставшиеся без ответа дольше самой проверки — запасной путь."""
    out = []
    for req, chk in await _open(session, server, now):
        if req.taken_at is not None and (
            (now - _aware(req.taken_at)).total_seconds() < _timeout_s(chk) + 10
        ):
            continue
        out.append(site_probe_task(chk, -req.id))
    return out


async def ingest(
    session: AsyncSession, server_id: int, results: list, now: datetime
) -> list:
    """Раскладывает ответы агента на ручные проверки (номера < 0).

    Возвращает очередь алертов: успех закрывает инцидент, и если о падении уже
    написали, нужен отбой. Не коммитит."""
    pending: list = []
    for r in results:
        try:
            rid = -int(r.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if rid <= 0:
            continue
        req = await session.get(ProbeRequest, rid)
        if req is None or req.server_id != server_id or req.ts is not None:
            continue  # чужая, повтор уже учтённого ответа или давно вычищенная
        req.ts = now
        req.code = int(r.get("code") or 0)
        lat = r.get("latency_ms")
        req.latency_ms = int(lat) if isinstance(lat, (int, float)) else None
        req.error = str(r.get("error") or "")[:512]
        req.kw_up_found = bool(r.get("kw_up_found", True))
        req.kw_down_found = bool(r.get("kw_down_found", False))
        try:
            req.cert_expires = int(r.get("cert_expires") or 0)
        except (TypeError, ValueError):
            req.cert_expires = 0
        req.cert_issuer = str(r.get("cert_issuer") or "")[:128]

        if req.check_id == 0:
            await _ingest_domain(session, req, now)
            continue
        check = await session.get(Check, req.check_id)
        if check is None or not check.probe_local or check.probe_server_id != server_id:
            # пока ждали, монитор удалили, перевели на проверку из панели или сайт
            # переехал — ответ этой ноды про него больше ничего не значит
            continue

        outcome = checks_exec.outcome_from_agent(check, req, now, check.degraded_ms)
        req.status = outcome.status
        req.message = outcome.message[:512]
        await record_outcome(session, check, outcome, now, pending, manual=True)
        # срок сертификата — с того же соединения: человек мог жать кнопку ровно
        # после перевыпуска, и старый срок в карточке был бы враньём
        info = checks_exec.expiry_from_agent(check, req, now)
        if info.ssl_days is not None:
            check.ssl_days = info.ssl_days
            check.ssl_message = info.ssl_message[:256]

        probe = await session.get(AgentProbe, check.id)
        if probe is None:
            probe = AgentProbe(check_id=check.id, server_id=server_id, ts=now)
            session.add(probe)
        probe.server_id = server_id
        probe.ts = now
        probe.code = req.code
        probe.latency_ms = req.latency_ms
        probe.error = req.error
        probe.kw_up_found = req.kw_up_found
        probe.kw_down_found = req.kw_down_found
        probe.cert_expires = req.cert_expires
        probe.cert_issuer = req.cert_issuer
        probe.manual_until = now + timedelta(seconds=authority_seconds(check))
    return pending


async def _ingest_domain(session: AsyncSession, req: ProbeRequest, now: datetime) -> None:
    """Ответ агента про домен из мастера: вердикт — в его строку domain_probes.

    Ни монитора, ни инцидентов тут нет. Строку ищем по номеру запроса: если домен
    успели перепроверить, у строки уже другой запрос, и опоздавший ответ её не тронет."""
    chk = domain_check(req.url)
    outcome = checks_exec.outcome_from_agent(chk, req, now, chk.degraded_ms)
    req.status = outcome.status
    req.message = outcome.message[:512]
    row = await session.scalar(select(DomainProbe).where(DomainProbe.local_request_id == req.id))
    if row is None:
        return
    row.local_ts = now
    row.local_status = outcome.status
    row.local_latency_ms = outcome.latency_ms
    row.local_message = outcome.message[:512]
