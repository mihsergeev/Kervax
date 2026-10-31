"""Версия удаленной учетки не обнуляется, даже если SQLite повторно выдаст ее id."""

import base64
import hashlib

from sqlalchemy import BigInteger, case, cast, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import AppSetting, User


PREFIX = "auth-rev:"


def _key(username: str) -> str:
    # Полный SHA-256 помещается в key(64) вместе с префиксом. Это индекс имени,
    # не секрет и не хеш пароля.
    digest = hashlib.sha256(username.encode()).digest()
    return PREFIX + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _version(value: str) -> int:
    version = int(value)
    if version < 0:
        raise ValueError("Отрицательная версия отзыва учетной записи")
    return version


async def initial_version(session, username: str) -> int:
    row = await session.get(AppSetting, _key(username))
    return _version(row.value) if row is not None else 0


async def merge_revocation(session, key: str, value: str) -> None:
    """Отзыв нельзя откатить восстановлением старого конфигурационного бэкапа."""
    version = _version(value)
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(AppSetting).values(key=key, value=str(version))
    # Один UPSERT: параллельный restore и delete не могут затереть более новый отзыв.
    await session.execute(statement.on_conflict_do_update(
        index_elements=[AppSetting.key],
        set_={"value": case((cast(AppSetting.value, BigInteger) < version, str(version)),
                            else_=AppSetting.value)},
    ))


async def retire(session, user: User) -> bool:
    # Читаем и увеличиваем именно текущую версию в БД, а не снимок ORM-объекта:
    # смена пароля/logout могли успеть увеличить ее после чтения delete-запросом.
    # UPDATE блокирует запись до конца этой же транзакции с сохранением seed и DELETE.
    # Между SELECT и UPDATE SQLite мог отдать тот же id совершенно другой учетке.
    # Ее нельзя ни отзывать под чужим именем, ни удалять через устаревший ORM-объект.
    version = await session.scalar(update(User).where(
        User.id == user.id, User.username == user.username,
    ).values(
        token_version=User.token_version + 1,
    ).returning(User.token_version).execution_options(synchronize_session=False))
    if version is None:
        return False
    await merge_revocation(session, _key(user.username), str(version))
    return True
