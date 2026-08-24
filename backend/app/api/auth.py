import secrets
import string

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app import alerts, audit, ratelimit, settings_store, totp
from app.config import get_settings
from app.deps import CurrentUser, SessionDep
from app.models import User
from app.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    TelegramLinkOut,
    TelegramOut,
    TelegramUpdate,
    TokenResponse,
    TwoFASetupOut,
    TwoFAStatusOut,
    TwoFAVerifyRequest,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# фиктивный хэш для сравнения при несуществующем юзере — выравнивает время
# ответа, чтобы по задержке нельзя было перечислять логины
_DUMMY_HASH = hash_password("no-such-user-timing-guard")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _record_failure(session, settings, key: str, username: str, action: str) -> None:
    """Пишет неудачный вход в журнал; на переходе в блокировку — алерт (брутфорс)."""
    locked_now = await ratelimit.record_failure(session, key)
    await audit.record(session, username, action, key)
    if locked_now:
        await audit.record(
            session, username, "login_lockout", key,
            f"{ratelimit.MAX_FAILURES} неудачных попыток",
        )
        await alerts.security_alert(
            session, settings,
            f"🚨 Kervax: {ratelimit.MAX_FAILURES} неудачных попыток входа подряд "
            f"с IP {key} — вход временно заблокирован.",
        )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, session: SessionDep
) -> TokenResponse:
    settings = get_settings()
    key = _client_key(request)
    if await ratelimit.is_locked(session, key):
        await audit.record(session, body.username, "login_blocked", key, "rate-limited")
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много попыток входа — подождите несколько минут",
        )
    user = await session.scalar(select(User).where(User.username == body.username))
    # verify_password вызываем всегда (в т.ч. по фиктивному хэшу), чтобы время
    # ответа не зависело от существования логина
    password_ok = verify_password(
        body.password, user.password_hash if user else _DUMMY_HASH
    )
    if user is None or not password_ok:
        await _record_failure(session, settings, key, body.username, "login_fail")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    if user.totp_enabled:
        # detail — машиночитаемый маркер для фронта (показать поле кода)
        if not body.otp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "2fa_required")
        counter = totp.matched_counter(user.totp_secret, body.otp)
        # отвергаем неверный код И уже использованный (защита от replay)
        if counter is None or counter <= user.totp_last_counter:
            await _record_failure(session, settings, key, body.username, "login_2fa_fail")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "2fa_invalid")
        user.totp_last_counter = counter
        await session.commit()  # фиксируем счётчик ДО audit (у audit свой rollback)
    await ratelimit.clear(session, key)
    token = create_access_token(
        user.username, settings.jwt_secret, settings.jwt_ttl_minutes,
        user.token_version,
    )
    await audit.record(session, user.username, "login_ok", key)
    return TokenResponse(access_token=token)


@router.post("/password", response_model=TokenResponse)
async def change_password(
    body: PasswordChangeRequest, request: Request, user: CurrentUser, session: SessionDep
) -> TokenResponse:
    """Смена пароля из UI: проверяет текущий, инвалидирует все старые токены."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный текущий пароль")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1  # все ранее выданные токены становятся недействительны
    await session.commit()
    await audit.record(session, user.username, "password_change", user.username)
    settings = get_settings()
    await alerts.security_alert(
        session, settings,
        f"🔑 Kervax: пароль администратора «{user.username}» изменён "
        f"(IP {_client_key(request)}).",
    )
    token = create_access_token(
        user.username, settings.jwt_secret, settings.jwt_ttl_minutes,
        user.token_version,
    )
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.get("/2fa", response_model=TwoFAStatusOut)
async def twofa_status(user: CurrentUser) -> TwoFAStatusOut:
    return TwoFAStatusOut(enabled=user.totp_enabled)


@router.post("/2fa/setup", response_model=TwoFASetupOut)
async def twofa_setup(user: CurrentUser, session: SessionDep) -> TwoFASetupOut:
    if user.totp_enabled:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "2FA уже включена — сначала отключите её",
        )
    secret = totp.random_secret()
    user.totp_secret = secret  # ожидающий подтверждения секрет (enabled ещё False)
    await session.commit()
    return TwoFASetupOut(
        secret=secret,
        otpauth_uri=totp.provisioning_uri(secret, user.username),
    )


@router.post("/2fa/enable", response_model=TwoFAStatusOut)
async def twofa_enable(
    body: TwoFAVerifyRequest, user: CurrentUser, session: SessionDep
) -> TwoFAStatusOut:
    if user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "2FA уже включена")
    if not user.totp_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Сначала запросите настройку (setup)"
        )
    if not totp.verify(user.totp_secret, body.otp):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный код")
    user.totp_enabled = True
    await session.commit()
    await audit.record(session, user.username, "2fa_enable", user.username)
    return TwoFAStatusOut(enabled=True)


@router.post("/2fa/disable", response_model=TwoFAStatusOut)
async def twofa_disable(
    body: TwoFAVerifyRequest, user: CurrentUser, session: SessionDep
) -> TwoFAStatusOut:
    if not user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "2FA не включена")
    if not totp.verify(user.totp_secret, body.otp):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный код")
    user.totp_enabled = False
    user.totp_secret = ""
    await session.commit()
    await audit.record(session, user.username, "2fa_disable", user.username)
    return TwoFAStatusOut(enabled=False)


# --- персональные Telegram-алерты ---------------------------------------------
# Привязка сделана «кодом в чат», а не вводом chat id руками: узнать свой id без
# сторонних ботов человек не может, а перепутать чужой — запросто (алерты про твою
# инфраструктуру уходили бы незнакомцу). Панель забирает апдейты бота и ищет код.

_LINK_ALPHABET = string.ascii_uppercase + string.digits


async def _tg_api(session, user: User) -> tuple[str, str]:
    """(база api, токен) для этой учётки: свой бот либо общий бот панели."""
    cfg = await settings_store.get_alert_config(session, get_settings())
    api = (cfg.get("telegram_api") or "https://api.telegram.org").rstrip("/")
    return api, (user.tg_token or cfg.get("telegram_token") or "")


async def _bot_name(api: str, token: str) -> str:
    if not token:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(f"{api}/bot{token}/getMe")
            r.raise_for_status()
            return "@" + (r.json().get("result", {}).get("username") or "")
    except Exception:  # noqa: BLE001 — имя бота не критично, покажем пустым
        return ""


async def _tg_state(session, user: User) -> TelegramOut:
    api, token = await _tg_api(session, user)
    return TelegramOut(
        chat_id=user.tg_chat_id,
        own_token=bool(user.tg_token),
        alerts=user.tg_alerts,
        bot=await _bot_name(api, token),
        ready=bool(user.tg_chat_id and token),
    )


@router.get("/telegram", response_model=TelegramOut)
async def telegram_status(user: CurrentUser, session: SessionDep) -> TelegramOut:
    return await _tg_state(session, user)


@router.post("/telegram/link", response_model=TelegramLinkOut)
async def telegram_link(user: CurrentUser, session: SessionDep) -> TelegramLinkOut:
    """Выдаёт одноразовый код: его надо отправить боту, затем нажать «Проверить»."""
    api, token = await _tg_api(session, user)
    if not token:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Бот не настроен: укажите свой токен или попросите админа заполнить общий",
        )
    user.tg_link_code = "".join(secrets.choice(_LINK_ALPHABET) for _ in range(6))
    await session.commit()
    return TelegramLinkOut(code=f"/link {user.tg_link_code}", bot=await _bot_name(api, token))


@router.post("/telegram/confirm", response_model=TelegramOut)
async def telegram_confirm(user: CurrentUser, session: SessionDep) -> TelegramOut:
    """Ищет код в свежих сообщениях бота и запоминает чат отправителя.

    offset намеренно НЕ передаём: апдейты остаются в очереди Telegram, иначе
    привязка одного сотрудника «съедала» бы сообщения остальных."""
    if not user.tg_link_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сначала получите код")
    api, token = await _tg_api(session, user)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бот не настроен")
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(f"{api}/bot{token}/getUpdates", params={"limit": 100})
            r.raise_for_status()
            updates = r.json().get("result", [])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Не удалось опросить бота: {exc}"
        ) from exc
    chat = ""
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or {}
        if user.tg_link_code in (msg.get("text") or ""):
            chat = str((msg.get("chat") or {}).get("id") or "")
    if not chat:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Сообщение с кодом не найдено — отправьте его боту и попробуйте снова",
        )
    user.tg_chat_id = chat
    user.tg_link_code = ""
    await session.commit()
    await audit.record(session, user.username, "tg_link", chat)
    return await _tg_state(session, user)


@router.patch("/telegram", response_model=TelegramOut)
async def telegram_update(
    body: TelegramUpdate, user: CurrentUser, session: SessionDep
) -> TelegramOut:
    if body.alerts is not None:
        user.tg_alerts = body.alerts
    if body.chat_id is not None:
        user.tg_chat_id = body.chat_id.strip()
    if body.token is not None:
        user.tg_token = body.token.strip()
    await session.commit()
    await audit.record(session, user.username, "tg_update", user.username)
    return await _tg_state(session, user)


@router.post("/telegram/test", response_model=TelegramOut)
async def telegram_test(user: CurrentUser, session: SessionDep) -> TelegramOut:
    api, token = await _tg_api(session, user)
    if not (token and user.tg_chat_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram не привязан")
    try:
        await alerts.tg_send(
            api, token, user.tg_chat_id,
            f"✅ Kervax: проверка связи для {user.username}. "
            "Алерты по вашим серверам и сайтам будут приходить сюда.",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Не отправлено: {exc}") from exc
    return await _tg_state(session, user)


@router.delete("/telegram", response_model=TelegramOut)
async def telegram_unlink(user: CurrentUser, session: SessionDep) -> TelegramOut:
    user.tg_chat_id = ""
    user.tg_token = ""
    user.tg_link_code = ""
    await session.commit()
    await audit.record(session, user.username, "tg_unlink", user.username)
    return await _tg_state(session, user)
