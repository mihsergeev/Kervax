from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import case, delete as sa_delete, func, select

from app import audit, domain_probe, manual_probe
from app.checks import status_matches
from app import checks as checks_exec
from app.collector import (
    _aware,
    effective_locations,
    record_outcome,
    seen_online,
    send_alerts_soon,
)
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, group_allowed, scope_query
from app.models import (
    AgentProbe,
    Check,
    CheckIncident,
    CheckIpSample,
    CheckSample,
    Location,
    IgnoredDomain,
    LocationResult,
    LocationSample,
    ProbeRequest,
    Server,
    User,
)
from app.schemas import (
    LocalProbeSuggestionsOut,
    LocalProbeSuggestion,
    LocalProbeApplyIn,
    AdoptDomainsIn,
    AdoptItemOut,
    AdoptResult,
    BulkResult,
    CheckBulkUpdate,
    CheckCreate,
    CheckIdList,
    SnoozeIn,
    CheckImport,
    CheckHistoryOut,
    CheckIncidentOut,
    CheckOut,
    CheckReorder,
    CheckRunOut,
    CheckSampleOut,
    CheckUpdate,
    ChecksOverviewOut,
    DiscoveredDomain,
    DiscoveredOut,
    DomainProbeIn,
    DomainProbesOut,
    IgnoreDomainsIn,
    KnownHostsOut,
    LocationHealth,
    LocationResultOut,
    UptimeOut,
)

router = APIRouter(prefix="/checks", tags=["checks"])


def _hide_secrets(check: Check, user) -> Check:
    """Прячет секреты монитора от учётки, которая его всё равно не правит.

    auth_pass — пароль от закрытого раздела сайта, http_headers часто содержит
    «Authorization: Bearer …». Роль «только просмотр» заводят как раз для тех,
    кому доступ к таким вещам не выдавали."""
    if user.role in ("admin", "editor"):
        return check
    check.auth_pass = ""
    check.http_headers = ""
    return check


async def _get_or_404(
    check_id: int, session: SessionDep, user: User | None = None
) -> Check:
    check = await session.get(Check, check_id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Монитор не найден")
    # Монитор вне групп учётки — отвечаем «не найден», а не «запрещено»: иначе
    # перебором id можно узнать, какие ещё мониторы есть в панели. Список уже
    # фильтровался (scope_query), а точечный доступ по id — нет: учётка с одной
    # группой читала и правила чужие мониторы, зная только номер.
    if user is not None and not group_allowed(user, check.group_name, "sites"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Монитор не найден")
    return check


async def _execute_and_store(session: SessionDep, check: Check):
    """Исполняет монитор из панели сейчас и записывает результат той же дорожкой,
    что и планировщик: снимок, статус, инцидент. Возвращает (исход, момент, алерты)."""
    outcome = await checks_exec.run_check(check)
    now = datetime.now(timezone.utc)
    pending: list = []
    await record_outcome(session, check, outcome, now, pending, manual=True)
    await session.commit()
    await session.refresh(check)
    return outcome, now, pending


def _run_out(check: Check, user, **extra) -> CheckRunOut:
    """Ответ на ручную проверку. Секреты прячем на модели ответа, а не на объекте
    сессии: иначе «очистка» уехала бы в базу на ближайшем flush."""
    out = CheckRunOut.model_validate(check)
    if user.role not in ("admin", "editor"):
        out.auth_pass = ""
        out.http_headers = ""
    for key, value in extra.items():
        setattr(out, key, value)
    return out


@router.get("", response_model=list[CheckOut])
async def list_checks(user: CurrentUser, session: SessionDep) -> list[Check]:
    # учётке с нарезанными группами показываем только её мониторы
    rows = list(
        await session.scalars(
            scope_query(user, select(Check), Check, "sites").order_by(Check.sort_order, Check.id)
        )
    )
    # expunge_all: ниже правим поля объектов, а они привязаны к сессии — без отвязки
    # SQLAlchemy запишет «очистку» секретов обратно в базу на ближайшем flush
    session.expunge_all()
    return [_hide_secrets(c, user) for c in rows]


async def _uptime_24h(session) -> dict[int, float]:
    """uptime % за последние 24ч по каждому монитору (доля снимков со статусом up)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = await session.execute(
        select(
            CheckSample.check_id,
            func.count().label("total"),
            func.sum(case((CheckSample.status == "up", 1), else_=0)).label("up"),
        )
        .where(CheckSample.ts >= cutoff)
        .group_by(CheckSample.check_id)
    )
    out: dict[int, float] = {}
    for cid, total, up in rows.all():
        if total:
            out[cid] = round((up or 0) / total * 100, 1)
    return out


async def _recent_beats(session, n: int = 30) -> dict[int, list[str]]:
    """Последние n снимков по каждому монитору (хронологически) — для мини-ленты
    статуса в списке.

    Оконная функция без ограничения по времени читала ВСЮ таблицу снимков: на
    5.6 млн строк и 378 мониторах страница «Сайты» открывалась 8 секунд, при
    этом нужны последние 30 снимков на монитор. Режем окно по времени — дальше
    работает индекс (check_id, ts). Ширину окна берём от самого редкого
    интервала проверок, с запасом, но не больше недели: у монитора с часовым
    интервалом 30 снимков это чуть больше суток."""
    slowest = await session.scalar(select(func.max(Check.interval_seconds))) or 60
    window = min(int(slowest) * (n + 5), 7 * 24 * 3600)
    since = datetime.now(timezone.utc) - timedelta(seconds=window)
    rn = func.row_number().over(
        partition_by=CheckSample.check_id, order_by=CheckSample.ts.desc()
    ).label("rn")
    sub = select(
        CheckSample.check_id, CheckSample.status, CheckSample.ts, rn
    ).where(CheckSample.ts >= since).subquery()
    rows = await session.execute(
        select(sub.c.check_id, sub.c.status)
        .where(sub.c.rn <= n)
        .order_by(sub.c.check_id, sub.c.ts)  # ts ↑ → старое→новое (лента слева направо)
    )
    beats: dict[int, list[str]] = {}
    for cid, status in rows.all():
        beats.setdefault(cid, []).append(status)
    return beats


@router.get("/overview", response_model=ChecksOverviewOut)
async def overview(_: CurrentUser, session: SessionDep) -> ChecksOverviewOut:
    checks = list(
        await session.scalars(select(Check).order_by(Check.sort_order, Check.id))
    )
    # статусные счётчики — только по ВКЛЮЧЁННЫМ (выключенный «down» — не проблема);
    # выключенные считаем отдельно, чтобы их можно было найти плиткой-фильтром
    counts = {"up": 0, "degraded": 0, "down": 0, "unknown": 0}
    disabled = 0
    partial = 0
    for c in checks:
        if not c.enabled:
            disabled += 1
            continue
        counts[c.last_status if c.last_status in counts else "unknown"] += 1
        # loc_alerted непустой = набор локаций, из которых сайт не отвечает
        # (заполняет collector после дебаунса). Считаем отдельно: основная
        # проверка при этом обычно зелёная.
        # check_locations обязателен: у монитора с ВЫКЛЮЧЕННОЙ проверкой из локаций
        # loc_alerted мог остаться с тех пор, когда её включали, — и он висел бы
        # «частично» вечно, хотя из локаций его никто больше не проверяет.
        if c.check_locations and c.last_status != "down" and c.loc_alerted:
            partial += 1
    # имена локаций: чип в списке должен называть точку, а не считать её («из 1
    # локаций» не отвечает на вопрос, что сломалось)
    locs = {
        loc.id: loc.name
        for loc in await session.scalars(select(Location).where(Location.enabled.is_(True)))
    }
    loc_stat: dict[int, list[int]] = {lid: [0, 0] for lid in locs}  # id → [down, total]
    for c in checks:
        if not (c.enabled and c.check_locations):
            continue
        for lid in locs:
            loc_stat[lid][1] += 1
        for lid in c.loc_alerted or []:
            if lid in loc_stat:
                loc_stat[lid][0] += 1
    uptime = await _uptime_24h(session)
    beats = await _recent_beats(session)
    enabled_ids = [c.id for c in checks if c.enabled]
    open_inc = await session.scalar(
        select(func.count()).select_from(CheckIncident).where(
            CheckIncident.ended_at.is_(None),
            CheckIncident.check_id.in_(enabled_ids),
        )
    ) if enabled_ids else 0
    # имена серверов-проверяльщиков: в списке сайт помечается «локально · <сервер>»,
    # иначе зелёный статус выглядит как обычная внешняя доступность, а это не она
    pids = {c.probe_server_id for c in checks if c.probe_server_id}
    pnames = (
        {r[0]: r[1] for r in (await session.execute(
            select(Server.id, Server.name).where(Server.id.in_(pids))
        )).all()}
        if pids else {}
    )
    outs = []
    for c in checks:
        co = CheckOut.model_validate(c)
        co.probe_server_name = pnames.get(c.probe_server_id or 0)
        co.uptime_24h = uptime.get(c.id)
        co.beats = beats.get(c.id)
        co.loc_down = (
            [locs[lid] for lid in (c.loc_alerted or []) if lid in locs]
            if c.check_locations
            else []
        )
        outs.append(co)
    return ChecksOverviewOut(
        total=len(checks),
        up=counts["up"],
        degraded=counts["degraded"],
        down=counts["down"],
        unknown=counts["unknown"],
        disabled=disabled,
        partial=partial,
        loc_summary=[
            LocationHealth(id=lid, name=locs[lid], down=d, total=t)
            for lid, (d, t) in loc_stat.items()
            if d > 0
        ],
        open_incidents=open_inc or 0,
        checks=outs,
    )


@router.get("/incidents", response_model=list[CheckIncidentOut])
async def list_incidents(
    _: CurrentUser,
    session: SessionDep,
    check_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[CheckIncidentOut]:
    query = select(CheckIncident).order_by(CheckIncident.started_at.desc()).limit(limit)
    if check_id is not None:
        query = query.where(CheckIncident.check_id == check_id)
    incidents = list(await session.scalars(query))
    # порог и интервал нужны, чтобы объяснить, ПОЧЕМУ по инциденту не было алерта
    meta = {
        r[0]: r
        for r in (await session.execute(select(
            Check.id, Check.name, Check.alert_after_failures,
            Check.degraded_after_failures, Check.interval_seconds,
        ))).all()
    }
    out = []
    for inc in incidents:
        o = CheckIncidentOut.model_validate(inc)
        m = meta.get(inc.check_id)
        if m:
            o.check_name = m[1]
            o.alert_after = m[3] if inc.status == "degraded" else m[2]
            o.interval_seconds = m[4]
        out.append(o)
    return out


@router.patch("/bulk", response_model=BulkResult)
async def bulk_update(
    body: CheckBulkUpdate, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """Применяет переданные поля к мониторам. ids=None → ко всем; ids=[…] → только
    к выбранным (массовая настройка выделенной пачки)."""
    fields = body.model_dump(exclude_unset=True)
    ids = fields.pop("ids", None)  # ids — не поле монитора, а область применения
    if not fields:
        return BulkResult(updated=0)
    # scope_query: «ко всем» для учётки с нарезкой = ко всем ЕЁ мониторам, а не
    # ко всем в панели. Без него массовая правка дотягивалась до чужих групп.
    q = scope_query(user, select(Check), Check, "sites")
    if ids is not None:
        q = q.where(Check.id.in_(ids))
    checks = list(await session.scalars(q))
    for check in checks:
        for name, value in fields.items():
            setattr(check, name, value)
        if "probe_local" in fields or "check_locations" in fields:
            _one_probe_source(check)
    if "enabled" in fields:
        await _close_if_disabled(session, checks)
    await session.commit()
    scope = f"ids={len(ids)}" if ids is not None else "all"
    await audit.record(
        session, user.username, "checks_bulk", str(len(checks)), f"{scope}: {','.join(fields)}"
    )
    return BulkResult(updated=len(checks))


@router.post("/bulk-delete", response_model=BulkResult)
async def bulk_delete_checks(
    body: CheckIdList, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """Массовое удаление мониторов (с историей/инцидентами/локационными данными)."""
    rows = list(await session.scalars(
        scope_query(user, select(Check), Check, "sites").where(Check.id.in_(body.ids))
    ))
    if not rows:
        return BulkResult(updated=0)
    ids = [c.id for c in rows]
    names = ", ".join(c.name for c in rows[:20])
    for model in (CheckSample, CheckIncident, LocationResult, LocationSample, CheckIpSample,
                  AgentProbe, ProbeRequest):
        await session.execute(sa_delete(model).where(model.check_id.in_(ids)))
    await session.execute(sa_delete(Check).where(Check.id.in_(ids)))
    await session.commit()
    await audit.record(session, user.username, "checks_bulk_delete", str(len(ids)), names)
    return BulkResult(updated=len(ids))


@router.post("/reorder", response_model=BulkResult)
async def reorder_checks(
    body: CheckReorder, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """Ручной порядок мониторов (sort_order = позиция в списке) + опциональный
    перенос в другую группу (group_name)."""
    pos = {it.id: i for i, it in enumerate(body.order)}
    grp = {it.id: it.group_name for it in body.order if it.group_name is not None}
    # только свои мониторы: операция меняет и group_name, то есть чужой монитор
    # можно было бы перетащить в свою группу (и получить к нему полный доступ)
    checks = list(await session.scalars(scope_query(user, select(Check), Check, "sites")))
    for target in grp.values():
        if not group_allowed(user, target, "sites"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи"
            )
    updated = 0
    for check in checks:
        changed = False
        want = pos.get(check.id)
        if want is not None and check.sort_order != want:
            check.sort_order = want
            changed = True
        new_group = grp.get(check.id)
        if new_group is not None and check.group_name != new_group:
            check.group_name = new_group
            changed = True
        if changed:
            updated += 1
    await session.commit()
    await audit.record(session, user.username, "checks_reorder", str(len(pos)))
    return BulkResult(updated=updated)


@router.post("/import", response_model=BulkResult, status_code=status.HTTP_201_CREATED)
async def import_checks(
    body: CheckImport, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """Массово создаёт мониторы из списка. Первую проверку не гоняем (это сделает
    планировщик на ближайшем тике) — чтобы импорт большого списка был быстрым."""
    for item in body.items:
        if not group_allowed(user, item.group_name, "sites"):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи"
            )
    checks = [Check(**item.model_dump()) for item in body.items]
    session.add_all(checks)
    await session.commit()
    await audit.record(session, user.username, "checks_import", str(len(checks)))
    return BulkResult(updated=len(checks))


async def _first_check(session_factory, check_id: int) -> None:
    """Первая проверка в фоне (может быть медленной/висеть на недоступном сайте).

    Локальный сайт не трогаем: из панели его не видно, и первая же «проверка»
    записала бы ложный статус. Первый результат пришлёт агент — пока он в пути,
    планировщик не судит (прогрев привязки)."""
    try:
        async with session_factory() as session:
            check = await session.get(Check, check_id)
            if check is not None and not check.probe_local:
                await _execute_and_store(session, check)
    except Exception:  # noqa: BLE001 — не критично
        pass


def _host_of(check: Check) -> str:
    """Хост, который монитор реально проверяет — чтобы сопоставить с доменом сервиса."""
    target = (check.target or "").strip().lower()
    if not target:
        return ""
    if check.type == "http":
        if "://" not in target:
            target = "http://" + target
        return (urlparse(target).hostname or "").strip(".")
    # tcp_port / cert: «host» либо «host:port»
    return target.split("://")[-1].split("/")[0].split(":")[0].strip(".")


def _norm_domain(raw: str) -> str:
    """Домен из отчёта агента → канонический хост (без схемы, пути, порта и точки)."""
    d = (raw or "").strip().lower().rstrip(".")
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/")[0]
    return d.split(":", 1)[0]


def _adopt_problem(domain: str) -> str:
    """Почему домен нельзя завести монитором («» = можно).

    В server_name/Ingress живут не только реальные хосты: маски (*.example.com),
    regexp (~^...), заглушки (_, default_server). Проверять их нечем — HTTP-монитору
    нужен конкретный адрес, иначе он честно упадёт и будет шуметь алертами."""
    if not domain:
        return "пустое имя"
    if "*" in domain:
        return "маска — нет конкретного хоста"
    if domain.startswith("~"):
        return "regexp в server_name"
    if "." not in domain:
        return "не доменное имя"
    if any(c in domain for c in " \t,;\\"):
        return "недопустимые символы"
    return ""


async def _hosts_map(user, session) -> tuple[dict[str, int], list[str]]:
    """Карта «домен → id монитора» (0 = вне видимости учётки) + группы сайтов."""
    hosts: dict[str, int] = {}
    groups: set[str] = set()
    for check in await session.scalars(select(Check)):
        if check.group_name:
            groups.add(check.group_name)
        host = _host_of(check)
        if not host:
            continue
        visible = group_allowed(user, check.group_name, "sites")
        # если домен покрыт несколькими мониторами, оставляем видимый — с него есть ссылка
        if host not in hosts or (visible and hosts[host] == 0):
            hosts[host] = check.id if visible else 0
    return hosts, sorted(groups)


@router.get("/known-hosts", response_model=KnownHostsOut)
async def known_hosts(user: CurrentUser, session: SessionDep) -> KnownHostsOut:
    """Домены, уже стоящие на мониторинге — «Сервисы» рисуют по ним галочки."""
    hosts, groups = await _hosts_map(user, session)
    ignored = sorted(await session.scalars(select(IgnoredDomain.domain)))
    return KnownHostsOut(hosts=hosts, groups=groups, ignored=ignored)


@router.get("/discovered", response_model=DiscoveredOut)
async def discovered(user: CurrentUser, session: SessionDep) -> DiscoveredOut:
    """Домены со ВСЕХ веб-серверов парка — источник для мастера «поставить на
    мониторинг». Ходить по нодам руками, чтобы найти непокрытый сайт, бессмысленно:
    агент их и так собирает (nginx/Apache/Caddy, Ingress, Gateway API).

    Ноды фильтруются группами учётки — чужую инфраструктуру не показываем."""
    hosts, groups = await _hosts_map(user, session)
    ignored = sorted(await session.scalars(select(IgnoredDomain.domain)))
    found: dict[str, set[str]] = {}
    for srv in await session.scalars(scope_query(user, select(Server), Server)):
        for web in (srv.last_report or {}).get("web_services") or []:
            for raw in web.get("sites") or []:
                domain = _norm_domain(raw)
                if domain:
                    found.setdefault(domain, set()).add(srv.name)
    return DiscoveredOut(
        domains=[
            DiscoveredDomain(domain=d, servers=sorted(names))
            for d, names in sorted(found.items())
        ],
        hosts=hosts,
        groups=groups,
        ignored=ignored,
    )


@router.post("/discovered/probe", response_model=DomainProbesOut)
async def probe_discovered(
    body: DomainProbeIn,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> DomainProbesOut:
    """Разово проверить найденные домены снаружи и изнутри их сервера (см. domain_probe).

    Проверяем только то, что мастер и так предлагает: домен найден на сервере этой
    учётки, годится в монитор и ещё не мониторится. Иначе кнопка превратилась бы в
    способ гонять панель и агентов по произвольным адресам."""
    servers = list(await session.scalars(scope_query(user, select(Server), Server)))
    serving = _serving_servers(servers)
    hosts, _ = await _hosts_map(user, session)
    names: list[str] = []
    for raw in body.domains:
        domain = _norm_domain(raw)
        if domain in names or domain not in serving or domain in hosts:
            continue
        if not _adopt_problem(domain):
            names.append(domain)
    now = datetime.now(timezone.utc)
    started = await domain_probe.start(
        session, names, serving, {s.id: s for s in servers}, user.username, body.force, now
    )
    await session.commit()
    if started:
        background.add_task(
            domain_probe.run_external, request.app.state.session_factory, started, now
        )
    labels = {s.id: s.name for s in servers}
    return DomainProbesOut(items=await domain_probe.rows_out(session, names, labels, now))


@router.get("/discovered/probes", response_model=DomainProbesOut)
async def discovered_probes(user: CurrentUser, session: SessionDep) -> DomainProbesOut:
    """Свежие итоги разовых проверок найденных доменов — мастер опрашивает, пока идут."""
    servers = list(await session.scalars(scope_query(user, select(Server), Server)))
    labels = {s.id: s.name for s in servers}
    now = datetime.now(timezone.utc)
    return DomainProbesOut(
        items=await domain_probe.rows_out(session, list(_serving_servers(servers)), labels, now)
    )


# Коды, которыми прокси говорит «ты не в списке»: сайт при этом ЖИВ. 401 не берём —
# это «представься», нормальная работа сайта с авторизацией, и решать за владельца,
# что 401 не проблема, панель не должна.
_CLOSED_CODES = (403,)


def _closed_code(check, message: str) -> int:
    """Код «не пущу», если внешняя проверка упёрлась именно в него (0 = не тот случай)."""
    for code in _CLOSED_CODES:
        if f"HTTP {code}" in message and not status_matches(code, check.expected_status):
            return code
    return 0


def _serving_servers(servers) -> dict[str, tuple[int, str]]:
    """Карта «домен → сервер, который его обслуживает» из отчётов агентов.

    Если домен нашёлся на нескольких нодах, берём первую по имени: предложение
    всё равно подтверждает человек, а гадать за него незачем."""
    found: dict[str, tuple[int, str]] = {}
    for srv in sorted(servers, key=lambda x: x.name):
        for web in (srv.last_report or {}).get("web_services") or []:
            for raw in web.get("sites") or []:
                domain = _norm_domain(raw)
                if domain and domain not in found:
                    found[domain] = (srv.id, srv.name)
    return found


@router.get("/local-probe-suggestions", response_model=LocalProbeSuggestionsOut)
async def local_probe_suggestions(
    user: CurrentUser, session: SessionDep
) -> LocalProbeSuggestionsOut:
    """Сайты, которые не отвечают панели, но живут на известной ей ноде.

    Почти всегда это белый список: снаружи соединение рвут, а сайт цел. Панель уже
    знает домены всех веб-серверов парка — значит может не ждать, пока человек
    сопоставит одно с другим, а предложить проверять такой сайт изнутри сервера.
    Само не назначаем: проверка изнутри отвечает на другой вопрос, чем внешняя, и
    подменять одну другой без ведома владельца нельзя."""
    servers = list(await session.scalars(scope_query(user, select(Server), Server)))
    serving = _serving_servers(servers)
    out: list[LocalProbeSuggestion] = []
    for check in await session.scalars(
        select(Check).where(
            Check.enabled.is_(True),
            Check.type == "http",
            Check.probe_local.is_(False),
            Check.last_status == "down",
        )
    ):
        if not group_allowed(user, check.group_name, "sites"):
            continue
        msg = (check.last_message or "")[:200]
        host = _host_of(check)
        # Сайт ОТВЕЧАЕТ кодом «не пущу» — значит он жив, и проверять изнутри нечего:
        # довольно считать этот код нормой. Так лечатся сайты за прокси, которое
        # закрывает их само (Envoy Gateway, nginx с allow/deny), где локальной
        # проверке и зацепиться не за что: на localhost там никто не слушает.
        code = _closed_code(check, msg)
        if code:
            out.append(LocalProbeSuggestion(
                check_id=check.id, name=check.name, host=host, message=msg,
                kind="code", code=code,
            ))
            continue
        hit = serving.get(host)
        if not hit:
            continue
        out.append(LocalProbeSuggestion(
            check_id=check.id, name=check.name, host=host, message=msg,
            kind="local", server_id=hit[0], server_name=hit[1],
        ))
    out.sort(key=lambda x: (x.kind, x.name))
    return LocalProbeSuggestionsOut(items=out)


@router.post("/local-probe-apply", response_model=BulkResult)
async def local_probe_apply(
    body: LocalProbeApplyIn, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """Назначает мониторам проверку с той ноды, что обслуживает их домен.

    Сервер вычисляем ЗДЕСЬ, а не берём из запроса: иначе редактор мог бы назначить
    проверку с чужой ноды и увидеть ответ сайта, к которому доступа не имеет."""
    servers = list(await session.scalars(scope_query(user, select(Server), Server)))
    serving = _serving_servers(servers)
    updated = 0
    for check in await session.scalars(
        select(Check).where(Check.id.in_(body.check_ids), Check.type == "http")
    ):
        if not group_allowed(user, check.group_name, "sites"):
            continue
        msg = (check.last_message or "")[:200]
        code = _closed_code(check, msg)
        if code:
            # Дописываем код к ожидаемым, а не заменяем: «200-399» остаётся в силе,
            # и сайт, который однажды откроется, зелёным быть не перестанет.
            spec = (check.expected_status or "200-399").strip()
            check.expected_status = f"{spec},{code}" if spec else f"200-399,{code}"
            updated += 1
            continue
        hit = serving.get(_host_of(check))
        if not hit:
            continue
        check.probe_local = True
        check.probe_server_id = hit[0]
        updated += 1
    await session.commit()
    if updated:
        await audit.record(session, user.username, "local_probe_apply", str(body.check_ids))
    return BulkResult(updated=updated)


@router.post("/discovered/ignore", response_model=BulkResult)
async def ignore_domains(
    body: IgnoreDomainsIn, user: CurrentUser, session: SessionDep
) -> BulkResult:
    """«Этот домен мониторить не нужно» — убрать из предложений (или вернуть).

    Список общий на панель, а не личный: решение «дев-стенд мониторить не надо»
    относится к инфраструктуре, и каждому сотруднику отмахиваться от одного и того
    же было бы издевательством."""
    names = {_norm_domain(d) for d in body.domains}
    names.discard("")
    if not names:
        return BulkResult(updated=0)
    if body.ignore:
        have = set(await session.scalars(
            select(IgnoredDomain.domain).where(IgnoredDomain.domain.in_(names))
        ))
        fresh = names - have
        session.add_all([
            IgnoredDomain(domain=d, by_user=user.username) for d in sorted(fresh)
        ])
        changed = len(fresh)
    else:
        res = await session.execute(
            sa_delete(IgnoredDomain).where(IgnoredDomain.domain.in_(names))
        )
        changed = res.rowcount or 0
    await session.commit()
    await audit.record(
        session,
        user.username,
        "domains_ignore" if body.ignore else "domains_unignore",
        ", ".join(sorted(names)[:20]),
    )
    return BulkResult(updated=changed)


@router.post("/adopt", response_model=AdoptResult, status_code=status.HTTP_201_CREATED)
async def adopt_domains(
    body: AdoptDomainsIn, user: CurrentUser, session: SessionDep
) -> AdoptResult:
    """Заводит http-мониторы по доменам веб-сервиса (кнопка «+» в «Сервисах»).

    Первую проверку не гоняем (как в импорте) — планировщик заберёт на ближайшем тике,
    иначе добавление сотни доменов висело бы на самом медленном из них."""
    group = body.group_name.strip()
    if not group_allowed(user, group, "sites"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи"
        )
    # учётке, ограниченной группами, пустая группа означала бы «создал и не вижу» —
    # кладём в первую разрешённую
    if not group and (user.site_groups or []):
        group = (user.site_groups or [])[0]

    hosts, _ = await _hosts_map(user, session)
    max_order = await session.scalar(select(func.max(Check.sort_order))) or 0
    skipped: list[str] = []
    fresh: list[Check] = []
    seen: set[str] = set()
    # «Проверять изнутри» — ноду вычисляем здесь, а не берём из запроса (как и в
    # local_probe_apply): иначе редактор назначил бы проверку с чужой ноды.
    local = {_norm_domain(d) for d in body.local}
    serving = (
        _serving_servers(list(await session.scalars(scope_query(user, select(Server), Server))))
        if local else {}
    )
    now = datetime.now(timezone.utc)
    # итог по каждому домену: без него мастер после добавления не может показать,
    # что заведено, как оно будет проверяться и что нет — только общие числа
    items: list[AdoptItemOut] = []
    for raw in body.domains:
        domain = _norm_domain(raw)
        problem = _adopt_problem(domain)
        if problem:
            skipped.append(f"{raw} — {problem}")
            items.append(AdoptItemOut(domain=raw, reason="invalid", problem=problem))
            continue
        if domain in hosts or domain in seen:
            skipped.append(f"{domain} — уже в мониторинге")
            # повтор в самом запросе — строка по домену уже есть
            if domain not in seen:
                items.append(
                    AdoptItemOut(domain=domain, check_id=hosts[domain], reason="monitored")
                )
            continue
        seen.add(domain)
        max_order += 1
        check = Check(
            name=domain,
            type="http",
            target=domain_probe.target(domain),
            group_name=group,
            sort_order=max_order,
        )
        hit = serving.get(domain) if domain in local else None
        if hit:
            # домен, который панель не видит снаружи (так показала проверка в мастере),
            # проверяет агент его ноды; сразу помечаем привязку — первый ответ агента
            # ещё в пути, и это не падение сайта
            check.probe_local = True
            check.probe_server_id = hit[0]
            check.probe_bound_at = now
            _one_probe_source(check)
        fresh.append(check)
        items.append(AdoptItemOut(domain=domain, local=bool(hit), server=hit[1] if hit else ""))
    if fresh:
        session.add_all(fresh)
        await session.commit()
        await audit.record(
            session, user.username, "checks_adopt", ", ".join(c.name for c in fresh[:20])
        )
    made = {c.name: c.id for c in fresh}
    for it in items:
        if not it.reason:
            it.check_id = made[it.domain]
    hosts, _ = await _hosts_map(user, session)
    return AdoptResult(
        created=len(fresh), skipped=skipped, hosts=hosts,
        local=sum(1 for c in fresh if c.probe_local),
        group_name=group, items=items,
    )


@router.post("", response_model=CheckOut, status_code=status.HTTP_201_CREATED)
async def create_check(
    body: CheckCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> Check:
    # создавать в чужую группу нельзя: иначе учётка с нарезкой заводит мониторы,
    # которых сама не увидит, зато увидят соседи
    if not group_allowed(user, body.group_name, "sites"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи"
        )
    check = Check(**body.model_dump())
    _one_probe_source(check)
    # новый монитор — в конец списка (иначе прыгнул бы наверх при ручном порядке)
    max_order = await session.scalar(select(func.max(Check.sort_order)))
    check.sort_order = (max_order or 0) + 1
    session.add(check)
    await session.commit()
    await session.refresh(check)
    await audit.record(session, user.username, "check_create", check.name, check.type)
    # первую проверку — в фон, чтобы создание не висело на медленном/недоступном сайте
    background.add_task(_first_check, request.app.state.session_factory, check.id)
    return check


def _one_probe_source(check: Check) -> None:
    """Проверять можно ИЛИ изнутри сервера, ИЛИ из локаций, но не одновременно.

    Это один вопрос — откуда смотреть на сайт, — и раньше он задавался двумя
    независимыми галочками в разных концах формы. Включённые вместе они бессмысленны:
    сайт, закрытый снаружи белым списком (ради чего и нужна проверка изнутри), из
    локаций не увидит никто, и монитор получал вечную «частичную доступность».
    """
    if check.probe_local:
        check.check_locations = False
    else:
        # Галочку сняли — привязка к ноде больше ничего не значит. Раньше она
        # оставалась, и агент продолжал проверять сайт, который уже проверяет панель.
        check.probe_server_id = None
        check.probe_bound_at = None


async def _close_if_disabled(session, checks: list[Check]) -> None:
    """Выключенный монитор не проверяется, и открытый инцидент закрыть было бы
    некому: он висел вечно — «идёт сейчас» в карточке и «N откр. инцидентов» на
    главной у монитора, который никто не проверяет."""
    ids = [c.id for c in checks if not c.enabled]
    if not ids:
        return
    now = datetime.now(timezone.utc)
    for inc in await session.scalars(
        select(CheckIncident).where(
            CheckIncident.check_id.in_(ids), CheckIncident.ended_at.is_(None)
        )
    ):
        inc.ended_at = now


@router.get("/{check_id}", response_model=CheckOut)
async def get_check(check_id: int, user: CurrentUser, session: SessionDep) -> Check:
    check = await _get_or_404(check_id, session, user)
    session.expunge_all()
    return _hide_secrets(check, user)


@router.patch("/{check_id}", response_model=CheckOut)
async def update_check(
    check_id: int, body: CheckUpdate, user: CurrentUser, session: SessionDep
) -> Check:
    check = await _get_or_404(check_id, session, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(check, field, value)
    _one_probe_source(check)
    await _close_if_disabled(session, [check])
    await session.commit()
    await session.refresh(check)
    await audit.record(session, user.username, "check_update", check.name)
    return check


@router.post("/{check_id}/snooze", response_model=CheckOut)
async def snooze_check(
    check_id: int, body: SnoozeIn, user: CurrentUser, session: SessionDep
) -> Check:
    """Быстро приглушить алерты монитора на N часов (0 = снять)."""
    check = await _get_or_404(check_id, session, user)
    check.snooze_until = (
        datetime.now(timezone.utc) + timedelta(hours=body.hours) if body.hours > 0 else None
    )
    await session.commit()
    await session.refresh(check)
    await audit.record(
        session, user.username, "check_snooze", check.name, f"{body.hours}ч"
    )
    return check


@router.delete("/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_check(
    check_id: int, user: CurrentUser, session: SessionDep
) -> None:
    check = await _get_or_404(check_id, session, user)
    name = check.name
    await session.execute(
        sa_delete(CheckSample).where(CheckSample.check_id == check_id)
    )
    await session.execute(
        sa_delete(CheckIncident).where(CheckIncident.check_id == check_id)
    )
    await session.execute(
        sa_delete(LocationResult).where(LocationResult.check_id == check_id)
    )
    await session.execute(
        sa_delete(LocationSample).where(LocationSample.check_id == check_id)
    )
    await session.execute(
        sa_delete(CheckIpSample).where(CheckIpSample.check_id == check_id)
    )
    await session.execute(sa_delete(AgentProbe).where(AgentProbe.check_id == check_id))
    await session.execute(sa_delete(ProbeRequest).where(ProbeRequest.check_id == check_id))
    await session.delete(check)
    await session.commit()
    await audit.record(session, user.username, "check_delete", name)


@router.post("/{check_id}/run", response_model=CheckRunOut)
async def run_check_now(
    check_id: int,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
) -> CheckRunOut:
    """«Проверить сейчас» — проверка тем же путём, каким монитор проверяется всегда.

    Сайт, который проверяет агент на ноде, проверяем через агента: из панели его либо
    не видно, либо видно не то (панель в белом списке, а изнутри сайт сломан)."""
    check = await _get_or_404(check_id, session, user)
    if check.probe_local:
        return await _run_via_agent(check, user, session, request, background)
    # «Проверить сейчас» должно проверять СЕЙЧАС — в том числе срок домена и
    # сертификата, которые обычно берутся из кэша (регистратуры не любят частых
    # запросов). Иначе после оплаты домена панель ещё часами пишет «истёк».
    checks_exec.forget_domain(check.target)
    outcome, now, pending = await _execute_and_store(session, check)
    info = await checks_exec.probe_expiry(check)
    if info.domain_days is not None:
        check.domain_days = info.domain_days
        check.domain_message = info.domain_message[:256]
    if info.ssl_days is not None:
        check.ssl_days = info.ssl_days
        check.ssl_message = info.ssl_message[:256]
    if info.domain_days is not None or info.ssl_days is not None:
        check.expiry_checked_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(check)
    background.add_task(send_alerts_soon, request.app.state.session_factory, pending, now)
    return _run_out(
        check, user,
        run_status=outcome.status, run_message=outcome.message[:512],
        run_latency_ms=outcome.latency_ms, run_at=now, run_source="panel",
    )


async def _run_via_agent(check, user, session, request, background) -> CheckRunOut:
    now = datetime.now(timezone.utc)
    if check.type != "http":
        # агент изнутри умеет только HTTP-запрос к своему веб-серверу
        return _run_out(
            check, user, run_source="agent",
            run_error="проверка изнутри сервера бывает только у HTTP-мониторов — "
                      "переключите этот монитор на проверку из панели",
        )
    srv = await session.get(Server, check.probe_server_id) if check.probe_server_id else None
    if srv is None:
        # Ни одна нода не держит этот домен — это и есть честный ответ проверки.
        outcome = checks_exec.outcome_from_agent(check, None, now, check.degraded_ms)
        pending: list = []
        await record_outcome(session, check, outcome, now, pending, manual=True)
        await session.commit()
        await session.refresh(check)
        return _run_out(
            check, user, run_status=outcome.status, run_message=outcome.message[:512],
            run_at=now, run_source="agent",
        )
    base = {"probe_server_name": srv.name, "run_source": "agent", "run_server": srv.name}
    if not seen_online(srv, now):
        return _run_out(
            check, user, **base,
            run_error=f"сервер {srv.name} не на связи — проверить сайт изнутри сейчас "
                      "некому. Статус монитора не менялся.",
        )
    req = await session.scalar(
        select(ProbeRequest)
        .where(
            ProbeRequest.check_id == check.id,
            ProbeRequest.server_id == srv.id,
            ProbeRequest.ts.is_(None),
            ProbeRequest.created_at
            >= now - timedelta(seconds=manual_probe.deadline_seconds(check)),
        )
        .order_by(ProbeRequest.id.desc())
        .limit(1)
    )
    if req is None:  # повторное нажатие, пока ждём ответ, нового задания не плодит
        req = ProbeRequest(check_id=check.id, server_id=srv.id, created_at=now)
        session.add(req)
    srv.probe_pending_at = now
    await session.commit()
    await session.refresh(check)
    return _run_out(
        check, user, **base, run_pending=req.id, run_fast=manual_probe.fast_agent(srv),
    )


@router.get("/{check_id}/run/{request_id}", response_model=CheckRunOut)
async def run_check_result(
    check_id: int, request_id: int, user: CurrentUser, session: SessionDep
) -> CheckRunOut:
    """Итог ручной проверки через агента: ждём, готово или «проверить не удалось»."""
    check = await _get_or_404(check_id, session, user)
    req = await session.get(ProbeRequest, request_id)
    if req is None or req.check_id != check.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Проверка не найдена")
    srv = await session.get(Server, req.server_id)
    name = srv.name if srv else ""
    base = {"probe_server_name": name or None, "run_source": "agent", "run_server": name}
    if req.ts is not None:
        if not req.status:
            return _run_out(
                check, user, **base,
                run_error="пока шла проверка, монитор перестали проверять с этого "
                          "сервера — ответ не засчитан",
            )
        return _run_out(
            check, user, **base,
            run_status=req.status, run_message=req.message,
            run_latency_ms=None if req.error else req.latency_ms, run_at=req.ts,
        )
    now = datetime.now(timezone.utc)
    waited = (now - _aware(req.created_at)).total_seconds()
    if waited > manual_probe.deadline_seconds(check):
        return _run_out(
            check, user, **base,
            run_error=f"агент на сервере {name} не прислал ответ за {int(waited)} с — "
                      "проверка не выполнена. Статус монитора не менялся.",
        )
    return _run_out(
        check, user, **base, run_pending=req.id, run_fast=manual_probe.fast_agent(srv),
    )


def _bin_step(hours: float, interval: int) -> int:
    """Ширина бина: не уже интервала проверок и не больше ~300 точек на окно."""
    return max(interval, int(hours * 3600 // 300), 1)


def _bin_samples(samples, hours: float, interval: int) -> list[CheckSampleOut]:
    """Бинирует снимки по времени, чтобы payload графика оставался небольшим.
    В бакете: средняя latency/value и худший статус (для полосы статусов)."""
    step = _bin_step(hours, interval)
    buckets: dict[int, list] = {}
    for s in samples:
        b = int(s.ts.timestamp() // step) * step
        buckets.setdefault(b, []).append(s)
    points: list[CheckSampleOut] = []
    for b in sorted(buckets):
        grp = buckets[b]
        lats = [x.latency_ms for x in grp if x.latency_ms is not None]
        vals = [v for x in grp if (v := getattr(x, "value", None)) is not None]
        worst = "up"
        for x in grp:
            if x.status == "down":
                worst = "down"
                break
            if x.status == "degraded":
                worst = "degraded"
        points.append(
            CheckSampleOut(
                ts=datetime.fromtimestamp(b, timezone.utc),
                status=worst,
                latency_ms=round(sum(lats) / len(lats)) if lats else None,
                value=round(sum(vals) / len(vals), 1) if vals else None,
                message="",
            )
        )
    return points


@router.get("/{check_id}/history", response_model=CheckHistoryOut)
async def check_history(
    check_id: int,
    user: CurrentUser,
    session: SessionDep,
    hours: int = Query(default=24, ge=1, le=720),
    location_id: int | None = Query(default=None),
    ip: str | None = Query(default=None, max_length=64),  # график по конкретному IP
    from_ts: float | None = Query(default=None),  # unix-сек — произвольный диапазон (зум)
    to_ts: float | None = Query(default=None),
) -> CheckHistoryOut:
    check = await _get_or_404(check_id, session, user)
    now = datetime.now(timezone.utc)
    if from_ts is not None and to_ts is not None and to_ts > from_ts:
        lo = datetime.fromtimestamp(from_ts, timezone.utc)
        hi = datetime.fromtimestamp(to_ts, timezone.utc)
        span_hours = max((to_ts - from_ts) / 3600, 0.02)
    else:
        lo, hi, span_hours = now - timedelta(hours=hours), now, float(hours)

    # конкретный IP (режим «все адреса») → его тайм-серия
    if ip:
        ip_samples = list(
            await session.scalars(
                select(CheckIpSample)
                .where(
                    CheckIpSample.check_id == check_id,
                    CheckIpSample.ip == ip,
                    CheckIpSample.ts >= lo,
                    CheckIpSample.ts <= hi,
                )
                .order_by(CheckIpSample.ts)
            )
        )
        return CheckHistoryOut(
            check_id=check_id,
            interval_seconds=check.interval_seconds,
            points=_bin_samples(ip_samples, span_hours, check.interval_seconds),
            step_seconds=_bin_step(span_hours, check.interval_seconds),
        )

    # прокси-локация → её тайм-серия; прямая/без параметра → основная проверка
    loc = await session.get(Location, location_id) if location_id else None
    if loc is not None and loc.url:
        samples = list(
            await session.scalars(
                select(LocationSample)
                .where(
                    LocationSample.check_id == check_id,
                    LocationSample.location_id == location_id,
                    LocationSample.ts >= lo,
                    LocationSample.ts <= hi,
                )
                .order_by(LocationSample.ts)
            )
        )
        interval = max(get_settings().location_probe_interval, 1)
        return CheckHistoryOut(
            check_id=check_id,
            interval_seconds=interval,
            points=_bin_samples(samples, span_hours, interval),
            step_seconds=_bin_step(span_hours, interval),
        )

    samples = list(
        await session.scalars(
            select(CheckSample)
            .where(
                CheckSample.check_id == check_id,
                CheckSample.ts >= lo,
                CheckSample.ts <= hi,
            )
            .order_by(CheckSample.ts)
        )
    )
    return CheckHistoryOut(
        check_id=check_id,
        interval_seconds=check.interval_seconds,
        points=_bin_samples(samples, span_hours, check.interval_seconds),
        step_seconds=_bin_step(span_hours, check.interval_seconds),
    )


@router.get("/{check_id}/locations", response_model=list[LocationResultOut])
async def check_locations(
    check_id: int, user: CurrentUser, session: SessionDep
) -> list[LocationResultOut]:
    check = await _get_or_404(check_id, session, user)
    enabled = list(
        await session.scalars(
            select(Location).where(Location.enabled.is_(True)).order_by(Location.id)
        )
    )
    locations = effective_locations(check, enabled)
    results = {
        r.location_id: r
        for r in await session.scalars(
            select(LocationResult).where(LocationResult.check_id == check_id)
        )
    }
    out: list[LocationResultOut] = []
    for loc in locations:
        if not loc.url:  # прямая локация = основная проверка панели
            out.append(
                LocationResultOut(
                    location_id=loc.id, name=loc.name, direct=True,
                    status=check.last_status, latency_ms=check.last_latency_ms,
                    message=check.last_message,
                    checked_at=check.last_checked_at or check.created_at,
                )
            )
            continue
        r = results.get(loc.id)
        if r is None:  # прокси ещё не проверялся
            out.append(
                LocationResultOut(
                    location_id=loc.id, name=loc.name, status="unknown",
                    latency_ms=None, message="", checked_at=check.created_at,
                )
            )
        else:
            out.append(
                LocationResultOut(
                    location_id=loc.id, name=loc.name, status=r.status,
                    latency_ms=r.latency_ms, message=r.message,
                    checked_at=r.checked_at,
                )
            )
    return out


@router.get("/{check_id}/log", response_model=list[CheckSampleOut])
async def check_log(
    check_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
    failed: bool = Query(default=False),
) -> list[CheckSampleOut]:
    """Журнал проверок: сырые снимки с сообщениями (напр. «HTTP 403», «ReadTimeout»),
    новые сверху. failed=1 — только не-up (моменты недоступности/деградации)."""
    await _get_or_404(check_id, session, user)
    query = select(CheckSample).where(CheckSample.check_id == check_id)
    if failed:
        query = query.where(CheckSample.status != "up")
    query = query.order_by(CheckSample.ts.desc()).limit(limit)
    return [CheckSampleOut.model_validate(s) for s in await session.scalars(query)]


@router.get("/{check_id}/uptime", response_model=UptimeOut)
async def check_uptime(
    check_id: int, user: CurrentUser, session: SessionDep
) -> UptimeOut:
    await _get_or_404(check_id, session, user)
    now = datetime.now(timezone.utc)

    async def frac(hours: int) -> float | None:
        cutoff = now - timedelta(hours=hours)
        total, up = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(case((CheckSample.status == "up", 1), else_=0)),
                ).where(
                    CheckSample.check_id == check_id, CheckSample.ts >= cutoff
                )
            )
        ).one()
        return round((up or 0) / total * 100, 2) if total else None

    return UptimeOut(day=await frac(24), week=await frac(168), month=await frac(720))
