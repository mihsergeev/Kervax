from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from sqlalchemy import case, delete as sa_delete, func, select

from app import audit
from app import checks as checks_exec
from app.collector import effective_locations
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, group_allowed, scope_query
from app.models import (
    Check,
    CheckIncident,
    CheckIpSample,
    CheckSample,
    Location,
    IgnoredDomain,
    LocationResult,
    LocationSample,
    Server,
    User,
)
from app.schemas import (
    AdoptDomainsIn,
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
    CheckSampleOut,
    CheckUpdate,
    ChecksOverviewOut,
    DiscoveredDomain,
    DiscoveredOut,
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


async def _execute_and_store(session: SessionDep, check: Check) -> None:
    """Исполняет монитор сейчас, пишет снимок и обновляет последний статус."""
    outcome = await checks_exec.run_check(check)
    now = datetime.now(timezone.utc)
    session.add(
        CheckSample(
            check_id=check.id,
            status=outcome.status,
            latency_ms=outcome.latency_ms,
            value=outcome.value,
            message=outcome.message[:512],
            ts=now,
        )
    )
    check.last_status = outcome.status
    check.last_message = outcome.message[:512]
    check.last_latency_ms = outcome.latency_ms
    check.last_value = outcome.value
    check.last_checked_at = now
    if outcome.ip_results is not None:
        check.last_ip_results = outcome.ip_results
        for ipr in outcome.ip_results:
            session.add(CheckIpSample(
                check_id=check.id, ip=ipr["ip"], status=ipr["status"],
                latency_ms=ipr.get("latency_ms"), ts=now,
            ))
    await session.commit()
    await session.refresh(check)


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
    open_inc = await session.scalar(
        select(func.count()).select_from(CheckIncident).where(
            CheckIncident.ended_at.is_(None)
        )
    )
    outs = []
    for c in checks:
        co = CheckOut.model_validate(c)
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
    for model in (CheckSample, CheckIncident, LocationResult, LocationSample, CheckIpSample):
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
    """Первая проверка в фоне (может быть медленной/висеть на недоступном сайте)."""
    try:
        async with session_factory() as session:
            check = await session.get(Check, check_id)
            if check is not None:
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
    for raw in body.domains:
        domain = _norm_domain(raw)
        problem = _adopt_problem(domain)
        if problem:
            skipped.append(f"{raw} — {problem}")
            continue
        if domain in hosts or domain in seen:
            skipped.append(f"{domain} — уже в мониторинге")
            continue
        seen.add(domain)
        max_order += 1
        fresh.append(
            Check(
                name=domain,
                type="http",
                target=f"https://{domain}",
                group_name=group,
                sort_order=max_order,
            )
        )
    if fresh:
        session.add_all(fresh)
        await session.commit()
        await audit.record(
            session, user.username, "checks_adopt", ", ".join(c.name for c in fresh[:20])
        )
    hosts, _ = await _hosts_map(user, session)
    return AdoptResult(created=len(fresh), skipped=skipped, hosts=hosts)


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
    await session.delete(check)
    await session.commit()
    await audit.record(session, user.username, "check_delete", name)


@router.post("/{check_id}/run", response_model=CheckOut)
async def run_check_now(
    check_id: int, user: CurrentUser, session: SessionDep
) -> Check:
    check = await _get_or_404(check_id, session, user)
    # «Проверить сейчас» должно проверять СЕЙЧАС — в том числе срок домена и
    # сертификата, которые обычно берутся из кэша (регистратуры не любят частых
    # запросов). Иначе после оплаты домена панель ещё часами пишет «истёк».
    checks_exec.forget_domain(check.target)
    await _execute_and_store(session, check)
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
    return check


def _bin_samples(samples, hours: float, interval: int) -> list[CheckSampleOut]:
    """Бинирует снимки по времени, чтобы payload графика оставался небольшим.
    В бакете: средняя latency/value и худший статус (для полосы статусов)."""
    window = hours * 3600
    step = max(interval, int(window // 300), 1)  # не больше ~300 точек
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
