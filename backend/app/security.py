import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt


# Минимальная длина пароля учётки. Одно место на всю панель: правило живёт в
# схемах API, в проверке стартового пароля и в подсказках интерфейса, и раньше
# число «8» было вписано в каждое из них отдельно.
MIN_PASSWORD_LEN = 12


def generate_agent_token() -> str:
    """Случайный токен агента (~256 бит). Показывается один раз при регистрации."""
    return secrets.token_urlsafe(32)


def hash_agent_token(token: str) -> str:
    """SHA-256 токена: токен высокоэнтропийный, медленный bcrypt не нужен;
    в БД храним только хеш → её утечка не даёт рабочих токенов."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_access_token(
    username: str, secret: str, ttl_minutes: int, token_version: int = 0
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "ver": token_version,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> dict | None:
    """Возвращает payload (sub/ver/…) при валидной подписи и сроке, иначе None."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if not isinstance(payload.get("sub"), str):
        return None
    return payload
