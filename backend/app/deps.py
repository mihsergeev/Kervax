import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import impersonate
from app.config import get_settings
from app.db import get_session
from app.models import Impersonation, User
from app.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# отказ учетке только для просмотра на любое изменение. Ожидаемый: журнал ошибок панели его не
# пишет (app/panel_errors.py), фронт такие запросы и не шлет (api.ts)
VIEWER_READ_ONLY = "Учетная запись только для просмотра: изменения запрещены"

# изменяющие HTTP-методы (viewer'у запрещены)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# self-service своей учетки (пароль/2FA) - можно и viewer'у; вход не аутентифицирован.
# Ошибку интерфейса и "сообщить о проблеме" шлет любая учетка: это запись в журнал ошибок
# панели, а не изменение объектов
_SELF_SERVICE_PREFIX = ("/api/auth/", "/api/panel-errors/front", "/api/panel-errors/report")

# Чтение логов учеткой только для просмотра с галкой can_read_logs. Логи - это команда агенту
# через POST того же эндпоинта, что и рестарт, поэтому пропускаем не путь, а тип команды: только
# эти три пути и только эти действия. Остальное (restart, delete_pod, unit_fix...) - как раньше
LOG_ACTIONS = {
    "docker": frozenset({"logs"}),
    "kube": frozenset({"logs", "logs_prev"}),
    "backup": frozenset({"unit_journal"}),  # журнал юнита, всей системы или ядра
}
LOGS_CLOSED = "Логи закрыты для этой учетки, может открыть админ"
_LOG_CMD_PATH = re.compile(r"/api/servers/\d+/(docker|kube|backup)/command")


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
    # токен удаленной учетки с тем же именем (security.create_access_token)
    if "uid" in payload and payload["uid"] != user.id:
        raise unauthorized
    impersonate.set_acting(None)
    if payload.get("imp") is not None:
        await _check_impersonation(payload, user, session, unauthorized)
        # режим "посмотреть, как видит": любые изменения запрещены, включая свою учетку
        # пользователя (пароль, 2FA, Telegram) - см. app/impersonate.py
        if request.method in _WRITE_METHODS and not request.url.path.startswith(impersonate.ALLOWED_WRITES):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, impersonate.READ_ONLY.format(user=user.username)
            )
    # роль «наблюдатель»: любой изменяющий запрос запрещён (кроме своей учётки).
    # Проверка здесь ловит ВСЕ аутентифицированные эндпоинты разом — ни один
    # изменяющий маршрут её не обойдёт, т.к. все они зависят от CurrentUser.
    if (
        user.role not in ("admin", "editor")
        and request.method in _WRITE_METHODS
        and not request.url.path.startswith(_SELF_SERVICE_PREFIX)
        and not await _viewer_reads_logs(request, user)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, VIEWER_READ_ONLY)
    # Разделы: если учётке нарезан список, всё за его пределами закрыто И на чтение.
    # Проверка здесь, а не в каждом роутере: любой аутентифицированный маршрут зависит
    # от CurrentUser, поэтому обойти её нельзя — как и запрет записи выше.
    if not section_allowed(user, request.url.path):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Раздел недоступен для этой учётной записи"
        )
    return user


async def _viewer_reads_logs(request: Request, user: User) -> bool:
    """POST учетки только для просмотра - это чтение логов, которое ей разрешил админ? Тип
    команды берется из тела: FastAPI к этому моменту его уже прочитал, и обработчик разберет
    то же самое тело (там действие проверяется еще раз, viewer_command_ok). Без галки, на другом
    пути, с другим действием или с непонятным телом - нет."""
    m = _LOG_CMD_PATH.fullmatch(request.url.path)
    if m is None or request.method != "POST":
        return False
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - не JSON: не логи
        return False
    if not isinstance(body, dict) or body.get("action") not in LOG_ACTIONS[m.group(1)]:
        return False
    if not user.can_read_logs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, LOGS_CLOSED)
    return True


def viewer_command_ok(user: User, kind: str, action: str) -> None:
    """Вторая проверка в обработчике команды, по уже разобранному телу: учетке только для
    просмотра можно только чтение логов и только с галкой. Первая - в get_current_user."""
    if user.role in ("admin", "editor"):
        return
    if action not in LOG_ACTIONS[kind]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, VIEWER_READ_ONLY)
    if not user.can_read_logs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, LOGS_CLOSED)


async def _check_impersonation(payload: dict, user: User, session: AsyncSession, unauthorized) -> None:
    """Токен входа под чужой учеткой действует, пока: админ все еще админ и не менял пароль,
    пользователь не стал админом, а сеанс не закрыт ("Вернуться к себе") и не истек."""
    admin = await session.scalar(select(User).where(User.username == payload["imp"]))
    row = await session.get(Impersonation, payload.get("imp_id") or 0)
    if (
        admin is None
        or admin.role != "admin"
        # Имена можно завести заново после удаления: старый вход не относится к новым учеткам.
        # Токены прежнего формата без id безопасно закрываются; админ может начать вход заново.
        or payload.get("uid") != user.id
        or payload.get("imp_uid") != admin.id
        or payload.get("imp_ver", 0) != admin.token_version
        or user.role == "admin"
        or row is None
        or row.ended_at is not None
        or row.admin != admin.username
        or row.target != user.username
    ):
        raise unauthorized
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise unauthorized
    impersonate.set_acting(impersonate.Acting(row.id, admin.username, user.username, expires))


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
