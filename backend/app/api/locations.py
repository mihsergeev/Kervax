import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import case, delete as sa_delete, func, select

from app import audit, checks, collector, open_ports, settings_store
from app.deps import AdminUser, CurrentUser, SessionDep
from app.models import Check, CheckSample, Location, LocationResult, LocationSample
from app.schemas import (
    LocationCreate,
    LocationOut,
    LocationsOverview,
    LocationStat,
    LocationTest,
    LocationUpdate,
    LocSiteCell,
    LocSiteRow,
    ProxyTestResult,
)

router = APIRouter(prefix="/locations", tags=["locations"])

# столько сайтов в таблице "где недоступно": дальше это уже авария, а не таблица
_SITES_MAX = 300


@router.post("/test", response_model=ProxyTestResult)
async def test_location_proxy(body: LocationTest, _: CurrentUser) -> ProxyTestResult:
    """Проверяет, что прокси локации доступен (идёт ли через него трафик) — чтобы
    сохранять только рабочие. Пустой url = «напрямую» → всегда ок. У рабочего прокси
    заодно видно, в какой стране он выходит в интернет."""
    ok, message, latency = await checks.test_proxy(body.url)
    ip, cc = await checks.proxy_exit(body.url) if ok and body.url.strip() else ("", "")
    return ProxyTestResult(ok=ok, message=message, latency_ms=latency, exit_ip=ip, exit_country=cc)


def _mask_proxy(url: str) -> str:
    """Прячет user:pass в адресе прокси: http://user:pass@host → http://***@host.

    Сам адрес оставляем — по нему в UI видно, идёт монитор напрямую или через
    точку проверки; а вот пара логин/пароль в списке, доступном всем, лишняя."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if creds else url


async def _get_or_404(location_id: int, session: SessionDep) -> Location:
    loc = await session.get(Location, location_id)
    if loc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Локация не найдена")
    return loc


@router.get("", response_model=list[LocationOut])
async def list_locations(user: CurrentUser, session: SessionDep) -> list[Location]:
    rows = list(await session.scalars(select(Location).order_by(Location.id)))
    if user.role == "admin":
        return rows  # админ правит локации — ему нужен адрес целиком
    session.expunge_all()  # иначе маскировка уедет в базу при flush
    for loc in rows:
        loc.url = _mask_proxy(loc.url)
    return rows


@router.get("/overview", response_model=LocationsOverview)
async def locations_overview(_: AdminUser, session: SessionDep) -> LocationsOverview:
    """Для окна локаций: как каждая проверяет сейчас и за сутки, через какую смотрят порты нод
    и какие сайты откуда недоступны. Локация "напрямую" (пустой адрес) - это основная проверка
    с самой панели, ее числа - по основной проверке всех мониторов."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    locs = list(await session.scalars(select(Location).order_by(Location.id)))
    active = list(await session.scalars(select(Check).where(Check.enabled.is_(True)).order_by(Check.name)))
    results = {(r.check_id, r.location_id): r for r in await session.scalars(select(LocationResult))}
    # кого через какую прокси гоняет сборщик - по тем же правилам, что и он сам
    probing = [loc for loc in locs if loc.enabled and loc.monitors]
    via: dict[int, list[Check]] = {loc.id: [] for loc in locs}
    for c in active:
        if c.check_locations and c.type == "http":
            for loc in collector.effective_locations(c, probing):
                if loc.url:
                    via[loc.id].append(c)

    ok_expr = func.sum(case((LocationSample.status != "down", 1), else_=0))
    day = {
        lid: (n, okn, avg)
        for lid, n, okn, avg in await session.execute(
            select(LocationSample.location_id, func.count(), ok_expr, func.avg(LocationSample.latency_ms))
            .where(LocationSample.ts >= since)
            .group_by(LocationSample.location_id)
        )
    }
    main_n, main_ok, main_avg = (
        await session.execute(
            select(func.count(), func.sum(case((CheckSample.status != "down", 1), else_=0)), func.avg(CheckSample.latency_ms))
            .where(CheckSample.ts >= since)
        )
    ).one()

    def pct(n, okn) -> float | None:
        return round(100.0 * (okn or 0) / n, 1) if n else None

    def ms(avg) -> int | None:
        return int(round(avg)) if avg is not None else None

    stats: list[LocationStat] = []
    # сломанный - только тот, что планировщик подтвердил запросом через сам прокси: если прокси
    # отвечает, сайты недоступны из той страны по-настоящему, и их надо видеть в таблице
    try:
        confirmed = {int(k) for k in json.loads(await settings_store.get_raw(session, settings_store.LOC_BROKEN_KEY) or "{}")}
    except (ValueError, TypeError):
        confirmed = set()
    broken = collector.broken_location_ids(probing, active, results) & confirmed
    for loc in locs:
        if not loc.url.strip():
            stats.append(LocationStat(
                id=loc.id,
                checks=len(active),
                up=sum(c.last_status == "up" for c in active),
                degraded=sum(c.last_status == "degraded" for c in active),
                down=sum(c.last_status == "down" for c in active),
                up_pct=pct(main_n, main_ok),
                avg_ms=ms(main_avg),
                last_at=max((c.last_checked_at for c in active if c.last_checked_at), default=None),
            ))
            continue
        mine = via[loc.id]
        got = [r for c in mine if (r := results.get((c.id, loc.id))) is not None]
        n, okn, avg = day.get(loc.id, (0, 0, None))
        dead = loc.id in broken  # все отсюда недоступны, а напрямую часть отвечает - это прокси
        stats.append(LocationStat(
            id=loc.id,
            checks=len(mine),
            up=sum(r.status == "up" for r in got),
            degraded=sum(r.status == "degraded" for r in got),
            down=sum(r.status == "down" for r in got),
            up_pct=pct(n, okn),
            avg_ms=ms(avg),
            last_at=max((r.checked_at for r in got if r.checked_at), default=None),
            broken=dead,
            error=Counter(r.message for r in got).most_common(1)[0][0] if dead else "",
        ))

    sites: list[LocSiteRow] = []
    for c in active:
        cells = {
            loc.id: LocSiteCell(status=r.status, message=r.message, fails=r.consecutive_fails or 0)
            for loc in locs
            if c in via[loc.id] and (r := results.get((c.id, loc.id))) is not None
        }
        if c.last_status == "down" or any(
            x.status == "down" and lid not in broken for lid, x in cells.items()
        ):
            sites.append(LocSiteRow(
                check_id=c.id,
                name=c.name,
                group=c.group_name or "",
                main=LocSiteCell(status=c.last_status, message=c.last_message, fails=c.consecutive_fails or 0),
                cells=cells,
            ))
    port = open_ports.port_location(locs)
    return LocationsOverview(
        locations=stats,
        ports_location_id=port.id if port is not None else None,
        sites=sites[:_SITES_MAX],
    )


def _exit_fields(loc: Location, ip: str | None, cc: str | None) -> None:
    """Выход в интернет из проверки, которую окно сделало перед сохранением."""
    loc.exit_ip = (ip or "")[:64]
    loc.exit_country = (cc or "").upper()[:2]
    loc.exit_at = datetime.now(timezone.utc)


@router.post("", response_model=LocationOut, status_code=status.HTTP_201_CREATED)
async def create_location(
    body: LocationCreate, user: AdminUser, session: SessionDep
) -> Location:
    data = body.model_dump()
    ip, cc = data.pop("exit_ip"), data.pop("exit_country")
    loc = Location(**data)
    if ip or cc:
        _exit_fields(loc, ip, cc)
    session.add(loc)
    await session.commit()
    await session.refresh(loc)
    await audit.record(session, user.username, "location_create", loc.name)
    return loc


@router.post("/{location_id}/test", response_model=ProxyTestResult)
async def test_saved_location(location_id: int, _: AdminUser, session: SessionDep) -> ProxyTestResult:
    """Проверяет сохраненную локацию и запоминает, где она выходит в интернет. У локации
    "напрямую" это выход самой панели. Пустой ответ тоже запоминаем, чтобы не спрашивать снова."""
    loc = await _get_or_404(location_id, session)
    ok, message, latency = await checks.test_proxy(loc.url)
    ip, cc = await checks.proxy_exit(loc.url) if ok else ("", "")
    _exit_fields(loc, ip, cc)
    await session.commit()
    return ProxyTestResult(ok=ok, message=message, latency_ms=latency, exit_ip=ip, exit_country=cc)


@router.patch("/{location_id}", response_model=LocationOut)
async def update_location(
    location_id: int, body: LocationUpdate, user: AdminUser, session: SessionDep
) -> Location:
    loc = await _get_or_404(location_id, session)
    data = body.model_dump(exclude_unset=True)
    ip, cc = data.pop("exit_ip", None), data.pop("exit_country", None)
    moved = "url" in data and (data["url"] or "").strip() != (loc.url or "").strip()
    for field, value in data.items():
        setattr(loc, field, value)
    if ip is not None or cc is not None:
        _exit_fields(loc, ip, cc)
    elif moved:  # адрес сменили без проверки - старый выход уже не про эту локацию
        loc.exit_ip, loc.exit_country, loc.exit_at = "", "", None
    await session.commit()
    await session.refresh(loc)
    await audit.record(session, user.username, "location_update", loc.name)
    return loc


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    location_id: int, user: AdminUser, session: SessionDep
) -> None:
    loc = await _get_or_404(location_id, session)
    name = loc.name
    await session.execute(
        sa_delete(LocationResult).where(LocationResult.location_id == location_id)
    )
    await session.delete(loc)
    await session.commit()
    await audit.record(session, user.username, "location_delete", name)
