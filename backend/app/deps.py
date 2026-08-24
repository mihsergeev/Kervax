from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# изменяющие HTTP-методы (viewer'у запрещены)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# self-service своей учётки (пароль/2FA) — можно и viewer'у; вход не аутентифицирован
_SELF_SERVICE_PREFIX = "/api/auth/"


async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    session: SessionDep,
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Недействительный или отсутствующий токен"
    )
    if credentials is None:
        raise unauthorized
    payload = decode_access_token(credentials.credentials, get_settings().jwt_secret)
    if payload is None:
        raise unauthorized
    user = await session.scalar(select(User).where(User.username == payload["sub"]))
    if user is None:
        raise unauthorized
    # токен, выпущенный до смены пароля (иная version), больше не действителен
    if payload.get("ver", 0) != user.token_version:
        raise unauthorized
    # роль «наблюдатель»: любой изменяющий запрос запрещён (кроме своей учётки).
    # Проверка здесь ловит ВСЕ аутентифицированные эндпоинты разом — ни один
    # изменяющий маршрут её не обойдёт, т.к. все они зависят от CurrentUser.
    if (
        user.role not in ("admin", "editor")
        and request.method in _WRITE_METHODS
        and not request.url.path.startswith(_SELF_SERVICE_PREFIX)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Учётная запись только для просмотра — изменения запрещены",
        )
    # Разделы: если учётке нарезан список, всё за его пределами закрыто И на чтение.
    # Проверка здесь, а не в каждом роутере: любой аутентифицированный маршрут зависит
    # от CurrentUser, поэтому обойти её нельзя — как и запрет записи выше.
    if not section_allowed(user, request.url.path):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Раздел недоступен для этой учётной записи"
        )
    return user


# Какие разделы панели открывает тот или иной путь API. Ключ — префикс пути, значение —
# разделы, любой из которых достаточен. «Серверы/Докер/Кубер/Сервисы» ходят в один и тот
# же /api/servers, различить их на уровне API нельзя — поэтому доступ даётся, если открыт
# хотя бы один из них, а реальное ограничение данных делают ГРУППЫ (см. group_allowed).
_SECTION_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("/api/checks", ("sites",)),
    ("/api/locations", ("sites",)),
    ("/api/backup", ("backups",)),
    ("/api/servers", ("servers", "docker", "kuber", "services", "backups")),
)

ALL_SECTIONS = ("sites", "servers", "docker", "kuber", "services", "backups")


def user_sections(user: User) -> list[str]:
    """Разделы учётки; пустой список в БД означает «все» — разворачиваем явно,
    чтобы фронту не пришлось знать про это соглашение."""
    return [x for x in (user.sections or []) if x in ALL_SECTIONS] or list(ALL_SECTIONS)


def section_allowed(user: User, path: str) -> bool:
    if not (user.sections or []):
        return True
    allowed = set(user_sections(user))
    for prefix, need in _SECTION_PATHS:
        if path.startswith(prefix):
            return bool(allowed & set(need))
    return True  # общие маршруты (профиль, алерты, аудит) разделами не режем


def group_allowed(user: User, group_name: str | None, kind: str = "servers") -> bool:
    """Видна ли учётке группа. Пустой список = ограничений нет.

    kind различает наборы: у серверов и у сайтов имена групп свои собственные."""
    groups = (user.site_groups if kind == "sites" else user.server_groups) or []
    if not groups:
        return True
    return (group_name or "") in groups


def scope_query(user: User, query, model, kind: str = "servers"):
    """Дописывает к запросу фильтр по группам учётки (если он задан)."""
    groups = (user.site_groups if kind == "sites" else user.server_groups) or []
    if not groups:
        return query
    return query.where(model.group_name.in_(groups))


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_admin_user(user: CurrentUser) -> User:
    """Только для админ-действий (управление пользователями и т.п.)."""
    if user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Требуются права администратора"
        )
    return user


AdminUser = Annotated[User, Depends(get_admin_user)]
