from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete as sa_delete, select

from app import audit, checks
from app.deps import AdminUser, CurrentUser, SessionDep
from app.models import Location, LocationResult
from app.schemas import (
    LocationCreate,
    LocationOut,
    LocationTest,
    LocationUpdate,
    ProxyTestResult,
)

router = APIRouter(prefix="/locations", tags=["locations"])


@router.post("/test", response_model=ProxyTestResult)
async def test_location_proxy(body: LocationTest, _: CurrentUser) -> ProxyTestResult:
    """Проверяет, что прокси локации доступен (идёт ли через него трафик) — чтобы
    сохранять только рабочие. Пустой url = «напрямую» → всегда ок."""
    ok, message, latency = await checks.test_proxy(body.url)
    return ProxyTestResult(ok=ok, message=message, latency_ms=latency)


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


@router.post("", response_model=LocationOut, status_code=status.HTTP_201_CREATED)
async def create_location(
    body: LocationCreate, user: AdminUser, session: SessionDep
) -> Location:
    loc = Location(**body.model_dump())
    session.add(loc)
    await session.commit()
    await session.refresh(loc)
    await audit.record(session, user.username, "location_create", loc.name)
    return loc


@router.patch("/{location_id}", response_model=LocationOut)
async def update_location(
    location_id: int, body: LocationUpdate, user: AdminUser, session: SessionDep
) -> Location:
    loc = await _get_or_404(location_id, session)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(loc, field, value)
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
