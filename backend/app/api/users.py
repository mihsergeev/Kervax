"""Управление учётными записями (только для админа).

Именная учётка сотрудника задаётся тремя вещами:
  * роль — admin (всё, включая учётки и настройки), editor (правки), viewer (чтение);
  * разделы — какие вкладки видны и открываются (пусто = все);
  * группы — какие серверы и мониторы вообще видны (пусто = все).

Всё три ограничения проверяет зависимость get_current_user (см. app/deps.py), то
есть их нельзя обойти ни одним маршрутом: запрет записи и закрытые разделы ловятся
там же, а группы — в выборках и при доступе к объекту по id.
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app import audit
from app.deps import AdminUser, SessionDep
from app.models import Check, Server, User
from app.schemas import UserCreate, UserListItem, UserResetPassword, UserUpdate
from app.security import hash_password

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserListItem])
async def list_users(_: AdminUser, session: SessionDep) -> list[User]:
    return list(await session.scalars(select(User).order_by(User.id)))


@router.post("", response_model=UserListItem, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, admin: AdminUser, session: SessionDep
) -> User:
    exists = await session.scalar(
        select(User).where(User.username == body.username)
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такой логин уже есть")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        # пустой список храним как есть: он и означает «без ограничений»
        sections=body.sections,
        server_groups=body.server_groups,
        site_groups=body.site_groups,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await audit.record(session, admin.username, "user_create", user.username, user.role)
    return user


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int, body: UserResetPassword, admin: AdminUser, session: SessionDep
) -> None:
    """Админ сбрасывает пароль учётки (напр. наблюдателя). Инвалидирует её токены."""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Учётка не найдена")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # старые токены сброшенной учётки становятся недействительны
    await session.commit()
    await audit.record(session, admin.username, "user_reset_pw", user.username)


@router.patch("/{user_id}", response_model=UserListItem)
async def update_user(
    user_id: int, body: UserUpdate, admin: AdminUser, session: SessionDep
) -> User:
    """Правка роли и границ доступа. Пароль меняется отдельным маршрутом."""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Учётка не найдена")
    if body.role is not None and body.role != user.role:
        # Разжаловать последнего админа нельзя ровно по той же причине, что и удалить:
        # панель осталась бы без управления, и вернуть права было бы уже некому.
        if user.role == "admin":
            admins = await session.scalar(
                select(func.count()).select_from(User).where(User.role == "admin")
            )
            if (admins or 0) <= 1:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Нельзя снять роль с последнего администратора",
                )
        user.role = body.role
    if body.sections is not None:
        user.sections = body.sections
    if body.server_groups is not None:
        user.server_groups = body.server_groups
    if body.site_groups is not None:
        user.site_groups = body.site_groups
    await session.commit()
    await session.refresh(user)
    await audit.record(session, admin.username, "user_scope", user.username)
    return user


@router.get("/groups", response_model=dict[str, list[str]])
async def list_groups(_: AdminUser, session: SessionDep) -> dict[str, list[str]]:
    """Группы для выбора области — РАЗДЕЛЬНО по видам: у серверов и у сайтов свои
    наборы имён, и общий список означал бы «дай доступ к чему-то одноимённому»."""
    srv = await session.scalars(select(Server.group_name).distinct())
    chk = await session.scalars(select(Check.group_name).distinct())
    return {
        "servers": sorted({x for x in srv if x}),
        "sites": sorted({x for x in chk if x}),
    }


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int, admin: AdminUser, session: SessionDep
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Учётка не найдена")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить свою учётку")
    # нельзя удалить последнего админа — иначе панель останется без управления
    if user.role == "admin":
        admin_count = await session.scalar(
            select(func.count()).select_from(User).where(User.role == "admin")
        )
        if (admin_count or 0) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Нельзя удалить последнего администратора"
            )
    name = user.username
    await session.delete(user)
    await session.commit()
    await audit.record(session, admin.username, "user_delete", name)
