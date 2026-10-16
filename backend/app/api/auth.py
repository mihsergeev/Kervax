import ipaddress
import math
import secrets
import string
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import and_, func, or_, select

from app import alerts, audit, ratelimit, settings_store, totp
from app.config import get_settings
from app.deps import CurrentUser, SessionDep
from app.models import AuditLog, LoginBan, User
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


# адреса своих прокси: docker-сети, локальная сеть хоста
_PROXY_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
]


def _is_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in n for n in _PROXY_NETS)


def _client_key(request: Request) -> str:
    """Адрес того, кто входит. Панель стоит за своими прокси (caddy, за ним nginx фронтенда)
    в docker-сети, и адрес соединения - это прокси: все входы в журнале выглядели одним
    частным адресом, а блокировка после неудач запирала всех сразу, а не того, кто подбирает
    пароль. Каждый прокси дописывает в X-Forwarded-For, от кого пришел запрос: справа налево
    пропускаем свои прокси, первый чужой адрес - клиент. Заголовку верим, только когда
    соединение от своего прокси: снаружи его можно подставить любой."""
    host = request.client.host if request.client else "unknown"
    try:
        if not _is_proxy(ipaddress.ip_address(host)):
            return host
    except ValueError:
        return host
    chain = []
    for part in request.headers.get("x-forwarded-for", "").split(","):
        try:
            chain.append(ipaddress.ip_address(part.strip()))
        except ValueError:
            continue
    for ip in reversed(chain):
        if not _is_proxy(ip):
            return str(ip)
    # все из локальной сети: вход изнутри, левый адрес - тот, кого увидел первый прокси
    return str(chain[0]) if chain else host


def ban_words(minutes: int) -> str:
    return {15: "на 15 минут", 60: "на час", 360: "на 6 часов", 1440: "на сутки"}.get(
        minutes, f"на {minutes} мин")


async def _lockout_alert(session, settings, ban: LoginBan) -> None:
    """Алерт о подборе пароля. Один на адрес: повторная блокировка того же адреса за сутки идет
    без алерта (она и так на сутки). И не чаще раза в час: при подборе с сотни адресов иначе
    пришла бы сотня сообщений - кого заблокировали молча, перечисляет следующий алерт, а все
    видно в окне Пользователи."""
    others = select(LoginBan.id).where(LoginBan.id != ban.id)
    if await session.scalar(others.where(
            LoginBan.alerted.is_(True), LoginBan.created_at >= ban.created_at - timedelta(hours=1)).limit(1)):
        return
    quiet = list(await session.scalars(select(LoginBan).where(
        LoginBan.id != ban.id, LoginBan.alerted.is_(False), LoginBan.repeat.is_(False),
        LoginBan.created_at >= ban.created_at - timedelta(days=1)).order_by(LoginBan.id)))
    for b in [ban, *quiet]:
        b.alerted = True
    await session.commit()
    minutes = round((ban.until - ban.created_at).total_seconds() / 60)
    text = f"🚨 Kervax: {ban.failures} неудачных входов за 15 минут с адреса {ban.key}"
    if ban.logins:
        text += f" (логины: {ban.logins})"
    text += f" - адрес заблокирован {ban_words(minutes)}."
    keys = list(dict.fromkeys(b.key for b in quiet if b.key != ban.key))
    if keys:
        text += f"\nЗа сутки заблокированы еще: {', '.join(keys[:5])}"
        text += f" и еще {len(keys) - 5}." if len(keys) > 5 else "."
    text += "\nБелый список и разблокировка - в окне Пользователи, раздел Защита входа."
    await alerts.security_alert(session, settings, text)


async def _record_failure(
    session, settings, key: str, username: str, action: str, guard: ratelimit.Guard, trusted: bool,
) -> None:
    """Пишет неудачный вход в журнал и считает его; на переходе в блокировку - запись о ней и
    алерт (подбор пароля). Адрес из белого списка не считается: только запись в журнале."""
    if trusted:
        await audit.record(session, username, action, key, "адрес в белом списке")
        return
    ban = await ratelimit.record_failure(session, key, guard, username)
    await audit.record(session, username, action, key)
    if ban is None:
        return
    minutes = round((ban.until - ban.created_at).total_seconds() / 60)
    await audit.record(
        session, username, "login_lockout", key,
        f"{ban.failures} неудачных входов за 15 минут, блокировка {ban_words(minutes)}"
        + (" (повторно за сутки)" if ban.repeat else ""),
    )
    if not ban.repeat:
        await _lockout_alert(session, settings, ban)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, session: SessionDep
) -> TokenResponse:
    settings = get_settings()
    key = _client_key(request)
    guard = await ratelimit.load(session)
    trusted = guard.allowed(key) is not None
    ban = None if trusted else await ratelimit.active_ban(session, key)
    if ban is not None:
        await ratelimit.note_blocked(session, ban)
        left = (_aware(ban.until) - datetime.now(timezone.utc)).total_seconds()
        # маркер для страницы входа: она пишет, через сколько минут пробовать
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"locked:{max(1, math.ceil(left / 60))}")
    user = await session.scalar(select(User).where(User.username == body.username))
    # verify_password вызываем всегда (в т.ч. по фиктивному хэшу), чтобы время
    # ответа не зависело от существования логина
    password_ok = verify_password(
        body.password, user.password_hash if user else _DUMMY_HASH
    )
    if user is None or not password_ok:
        await _record_failure(session, settings, key, body.username, "login_fail", guard, trusted)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    if user.totp_enabled:
        # detail — машиночитаемый маркер для фронта (показать поле кода)
        if not body.otp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "2fa_required")
        counter = totp.matched_counter(user.totp_secret, body.otp)
        # отвергаем неверный код И уже использованный (защита от replay)
        if counter is None or counter <= user.totp_last_counter:
            await _record_failure(session, settings, key, body.username, "login_2fa_fail", guard, trusted)
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


# входы под учеткой: удачные, неудачные (пароль, код 2FA) и блокировки после перебора
_MY_LOGINS = ("login_ok", "login_fail", "login_2fa_fail", "login_blocked", "login_lockout")
_MY_FAILS = ("login_fail", "login_2fa_fail")
_MY_SHOWN = 10


def _aware(ts: datetime | None) -> datetime | None:
    return ts if ts is None or ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@router.get("/me/activity")
async def my_activity(user: CurrentUser, session: SessionDep) -> dict:
    """Окно "Моя учетка": последние входы под этой учеткой с адресами, неудачные попытки за
    сутки (не подбирают ли пароль) и когда менялся пароль - сам или сброс админом. Весь журнал
    видит только админ, здесь - только свое."""
    rows = (await session.execute(
        select(AuditLog.ts, AuditLog.action, AuditLog.target)
        .where(AuditLog.username == user.username, AuditLog.action.in_(_MY_LOGINS))
        .order_by(AuditLog.id.desc()).limit(_MY_SHOWN))).all()
    fails = await session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.username == user.username, AuditLog.action.in_(_MY_FAILS),
        AuditLog.ts >= datetime.now(timezone.utc) - timedelta(days=1))) or 0
    pw = await session.scalar(select(func.max(AuditLog.ts)).where(or_(
        and_(AuditLog.username == user.username, AuditLog.action == "password_change"),
        and_(AuditLog.action == "user_reset_pw", AuditLog.target == user.username))))
    return {
        "logins": [{"ts": _aware(ts).isoformat(), "action": a, "ip": ip or ""} for ts, a, ip in rows],
        "fails_24h": fails,
        "password_changed": _aware(pw).isoformat() if pw else None,
    }


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
        if user.tg_chat_id:
            return await _tg_state(session, user)  # код уже нашел бот панели
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сначала получите код")
    api, token = await _tg_api(session, user)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бот не настроен")
    cfg = await settings_store.get_alert_config(session, get_settings())
    if alerts.buttons_on(cfg) and token == cfg.get("telegram_token"):
        # апдейты общего бота читает сама панель (кнопки под алертами): getUpdates отсюда
        # получил бы 409, а код найдет и привяжет опрос бота
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Код еще не дошел до бота - отправьте его и нажмите еще раз через пару секунд",
        )
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
