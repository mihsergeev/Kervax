"""Управление учётными записями (только для админа).

Именная учётка сотрудника задаётся тремя вещами:
  * роль — admin (всё, включая учётки и настройки), editor (правки), viewer (чтение);
  * разделы — какие вкладки видны и открываются (пусто = все);
  * группы — какие серверы и мониторы вообще видны (пусто = все).

Всё три ограничения проверяет зависимость get_current_user (см. app/deps.py), то
есть их нельзя обойти ни одним маршрутом: запрет записи и закрытые разделы ловятся
там же, а группы — в выборках и при доступе к объекту по id.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, or_, select, update

from app import audit, impersonate
from app.api.auth import _client_key
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.models import AuditLog, Check, Impersonation, Server, User
from app.schemas import (
    ImpersonateOut,
    UserCardOut,
    UserCreate,
    UserListItem,
    UserResetPassword,
    UsersOverview,
    UserUpdate,
)
from app.security import create_impersonation_token, hash_password

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserListItem])
async def list_users(_: AdminUser, session: SessionDep) -> list[User]:
    return list(await session.scalars(select(User).order_by(User.id)))


# неудачные входы: неверный пароль и неверный код 2FA
_FAILS = ("login_fail", "login_2fa_fail")
_LOGINS = ("login_ok", "login_blocked", "login_lockout") + _FAILS
AUDIT_SHOWN = 500


def _aware(ts: datetime | None) -> datetime | None:
    # SQLite отдает время без пояса, а пишется оно в UTC
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@router.get("/overview", response_model=UsersOverview)
async def users_overview(_: AdminUser, session: SessionDep) -> UsersOverview:
    """Окно "Пользователи": учетки с последним входом, неудачными входами за сутки и числом
    действий за 30 дней, и журнал действий - больше его нигде не видно."""
    now = datetime.now(timezone.utc)
    users = list(await session.scalars(select(User).order_by(User.id)))
    last_id = (
        select(func.max(AuditLog.id)).where(AuditLog.action == "login_ok").group_by(AuditLog.username)
    )
    last = {
        name: (ts, ip)
        for name, ts, ip in await session.execute(
            select(AuditLog.username, AuditLog.ts, AuditLog.target).where(AuditLog.id.in_(last_id))
        )
    }
    day = now - timedelta(days=1)
    fails = {
        name: n
        for name, n in await session.execute(
            select(AuditLog.username, func.count())
            .where(AuditLog.action.in_(_FAILS), AuditLog.ts >= day)
            .group_by(AuditLog.username)
        )
    }
    fail_ips = await session.scalar(
        select(func.count(func.distinct(AuditLog.target))).where(
            AuditLog.action.in_(_FAILS), AuditLog.ts >= day
        )
    )
    acts = {
        name: n
        for name, n in await session.execute(
            select(AuditLog.username, func.count())
            .where(AuditLog.action.not_in(_LOGINS), AuditLog.ts >= now - timedelta(days=30))
            .group_by(AuditLog.username)
        )
    }
    cards = []
    for u in users:
        ts, ip = last.get(u.username, (None, ""))
        base = UserListItem.model_validate(u).model_dump()
        base["created_at"] = _aware(base["created_at"])
        cards.append(
            UserCardOut(
                **base,
                tg_linked=bool(u.tg_chat_id),
                last_login=_aware(ts),
                last_ip=ip or "",
                fails_24h=fails.get(u.username, 0),
                actions_30d=acts.get(u.username, 0),
            )
        )
    log = await session.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(AUDIT_SHOWN))
    return UsersOverview(
        users=cards,
        audit=await audit.readable(session, list(log)),
        fails_24h=sum(fails.values()),
        fail_ips_24h=int(fail_ips or 0),
    )


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
        can_read_logs=body.can_read_logs and body.role == "viewer",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await audit.record(session, admin.username, "user_create", user.username, user.role)
    if user.can_read_logs:
        await audit.record(session, admin.username, "user_logs_on", user.username)
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
    # Галка чтения логов имеет смысл только у учетки только для просмотра: другим ролям логи
    # открыты и так. При смене роли она снимается, чтобы при возврате к просмотру логи не
    # открылись сами собой
    logs = user.can_read_logs if body.can_read_logs is None else body.can_read_logs
    logs = logs and user.role == "viewer"
    logs_changed = logs != user.can_read_logs
    user.can_read_logs = logs
    await session.commit()
    await session.refresh(user)
    await audit.record(session, admin.username, "user_scope", user.username)
    if logs_changed:
        await audit.record(session, admin.username, "user_logs_on" if logs else "user_logs_off", user.username)
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
    # Закрываем и старые входы, привязанные только к имени. Это важно и для SQLite,
    # где после удаления последней учетки ее id может достаться новой.
    await session.execute(update(Impersonation).where(
        or_(Impersonation.admin == name, Impersonation.target == name),
        Impersonation.ended_at.is_(None),
    ).values(ended_at=datetime.now(timezone.utc)))
    await session.delete(user)
    await session.commit()
    await audit.record(session, admin.username, "user_delete", name)


# --- вход под чужой учеткой (app/impersonate.py) ------------------------------------------------


async def _close_open(session, admin: str, why: str, now: datetime) -> None:
    """Закрывает незакрытые входы админа под чужими учетками (новый вход заменяет прежний)."""
    rows = list(await session.scalars(select(Impersonation).where(
        Impersonation.admin == admin, Impersonation.ended_at.is_(None))))
    for r in rows:
        r.ended_at = now
    await session.commit()
    for r in rows:
        await audit.record(
            session, admin, "impersonate_stop", r.target, why if _aware(r.expires_at) > now else "истек срок")


@router.post("/{user_id}/impersonate", response_model=ImpersonateOut)
async def impersonate_start(
    user_id: int, request: Request, admin: AdminUser, session: SessionDep
) -> ImpersonateOut:
    """Войти под учеткой, чтобы увидеть панель ее глазами. Ни пароль и 2FA пользователя, ни свой
    пароль еще раз не нужны: админ и так может все, а здесь он получает меньше, чем у него есть, -
    только просмотр на час. Начало и конец в журнале действий."""
    settings = get_settings()
    key = _client_key(request)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Учётка не найдена")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Под своей учеткой вы и так")
    if user.role == "admin":
        # у админа те же права, что у вас: смотреть нечего, а действовать от его имени незачем
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Под другим администратором войти нельзя: у него те же права, что у вас")
    now = datetime.now(timezone.utc)
    await _close_open(session, admin.username, "начат новый вход", now)
    expires = now + timedelta(minutes=impersonate.IMP_TTL_MINUTES)
    row = Impersonation(admin=admin.username, target=user.username, expires_at=expires, ip=key[:64])
    session.add(row)
    await session.commit()
    await session.refresh(row)
    token = create_impersonation_token(
        user.username, user.token_version, admin.username, admin.token_version, row.id,
        settings.jwt_secret, expires, user_id=user.id, admin_id=admin.id,
    )
    await audit.record(
        session, admin.username, "impersonate_start", user.username,
        f"только просмотр, на {impersonate.IMP_TTL_MINUTES} мин, IP {key}")
    return ImpersonateOut(access_token=token, imp_id=row.id, username=user.username, expires_at=expires)


@router.post("/impersonation/{imp_id}/stop", status_code=status.HTTP_204_NO_CONTENT)
async def impersonate_stop(imp_id: int, admin: AdminUser, session: SessionDep) -> None:
    """"Вернуться к себе": браузер снова ходит с токеном админа и закрывает вход - токен входа
    под чужой учеткой перестает действовать сразу. Зовется и после истечения срока, тогда в
    журнале причина "истек срок"."""
    row = await session.get(Impersonation, imp_id)
    if row is None or row.admin != admin.username:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вход не найден")
    if row.ended_at is not None:
        return
    now = datetime.now(timezone.utc)
    expired = _aware(row.expires_at) <= now
    row.ended_at = _aware(row.expires_at) if expired else now
    await session.commit()
    await audit.record(
        session, admin.username, "impersonate_stop", row.target, "истек срок" if expired else "вернулся к себе")
