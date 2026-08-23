"""Алерты (Telegram / вебхук): отправка в каналы и security-события.

Пороговые алерты по мониторам/инцидентам добавляются в Этапе 3 — здесь базовый
транспорт (send_alert) и security-события (брутфорс, смена пароля и т.п.).
"""

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.config import Settings
from app.models import User

log = logging.getLogger("kervax.alerts")


class Msg(str):
    """Текст алерта + адресация: раздел панели и группа объекта.

    Наследник str сознательно: общий канал и весь существующий код работают с
    сообщением как со строкой, а персональная рассылка дополнительно читает
    section/group, чтобы понять, кого это касается. Так адресация появилась без
    переписывания трёх десятков мест, где алерты собираются."""

    section: str
    group: str

    def __new__(cls, text: str, section: str = "", group: str = "") -> "Msg":
        obj = super().__new__(cls, text)
        obj.section = section
        obj.group = group
        return obj


# разделы, чьи объекты живут в группах СЕРВЕРОВ (у сайтов свой набор имён групп)
_SERVER_SECTIONS = ("servers", "docker", "kuber", "services", "backups")


def _msg_scope(m: str) -> tuple[str, str]:
    return (getattr(m, "section", ""), getattr(m, "group", ""))


def wants(user: User, m: str) -> bool:
    """Касается ли алерт этого человека — ровно та же нарезка, что и в панели.

    Зону ответственности настраивают один раз (разделы + группы учётки), поэтому
    отдельной подписки на алерты нет: видишь объект в панели — получаешь про него
    алерт. Сообщение без адресации (section='') — общее, идёт всем подписанным."""
    section, group = _msg_scope(m)
    if not section:
        return True
    if (user.sections or []) and section not in (user.sections or []):
        return False
    groups = (
        (user.server_groups or [])
        if section in _SERVER_SECTIONS
        else (user.site_groups or [])
    )
    if not groups:
        return True
    # объект вне групп учётка не видит и в панели — значит и алерт ей не нужен
    return group in groups


def alerts_enabled(cfg: dict) -> bool:
    return bool(
        (cfg.get("telegram_token") and cfg.get("telegram_chat"))
        or cfg.get("webhook")
    )


async def tg_send(
    api: str, token: str, chat: str, text: str, parse_mode: str | None = None
) -> None:
    """Одно сообщение в Telegram. Бросает исключение — обработка на вызывающем."""
    base = (api or "https://api.telegram.org").rstrip("/")
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{base}/bot{token}/sendMessage", json=payload)
        r.raise_for_status()


async def send_alert(cfg: dict, text: str, parse_mode: str | None = None) -> list[str]:
    """Шлёт текст во все настроенные каналы. Возвращает список ошибок (пустой = ок).
    parse_mode="HTML" — для сайтовых алертов со ссылкой <a href> в тексте (тогда
    весь динамический контент обязан быть html-экранирован вызывающим)."""
    errors: list[str] = []
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
    if token and chat:
        try:
            await tg_send(cfg.get("telegram_api", ""), token, chat, text, parse_mode)
        except Exception as exc:  # noqa: BLE001 — алерт не должен ронять цикл
            errors.append(f"Telegram: {exc}")
            log.warning("Telegram-алерт не отправлен: %s", exc)

    if cfg.get("webhook"):
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(cfg["webhook"], json={"text": text})
                r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Webhook: {exc}")
            log.warning("Вебхук-алерт не отправлен: %s", exc)
    return errors


_DIGEST_LINES = 15  # сколько сообщений показать в дайджесте (остальные — счётчиком)


def _digest(messages: list[str]) -> str:
    head = messages[:_DIGEST_LINES]
    extra = len(messages) - len(head)
    body = "\n\n".join(head)
    if extra:
        body += f"\n\n…и ещё {extra}"
    return f"🌊 {len(messages)} алертов за раз —\n\n{body}"


async def send_personal(
    session_factory, cfg: dict, messages: list[str], threshold: int,
    parse_mode: str | None = None,
) -> None:
    """Персональная доставка: каждому — только его алерты (см. wants).

    Best-effort и НИКОГДА не влияет на возвращаемое dispatch значение: иначе
    один сотрудник с отозванным ботом заставлял бы панель бесконечно перепосылать
    один и тот же алерт всем остальным."""
    try:
        async with session_factory() as session:
            users = list(
                await session.scalars(
                    select(User).where(User.tg_chat_id != "", User.tg_alerts.is_(True))
                )
            )
    except Exception:  # noqa: BLE001
        log.warning("не удалось прочитать получателей персональных алертов", exc_info=True)
        return
    if not users:
        return
    api = cfg.get("telegram_api", "")
    common = cfg.get("telegram_token") or ""
    for user in users:
        token = user.tg_token or common
        if not token:
            continue
        mine = [m for m in messages if wants(user, m)]
        if not mine:
            continue
        batch = [_digest(mine)] if threshold and len(mine) >= threshold else mine
        for text in batch:
            try:
                await tg_send(api, token, user.tg_chat_id, text, parse_mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("персональный алерт для %s не отправлен: %s", user.username, exc)
                break  # чат недоступен — остальные сообщения этому же адресату тоже


async def dispatch(
    cfg: dict,
    can_send: bool,
    messages: list[str],
    threshold: int,
    parse_mode: str | None = None,
    session_factory=None,
) -> bool:
    """Отправка пачки алертов ОДНОГО цикла с антифлудом. Если сообщений
    ≥ threshold (>0) — шлём один дайджест вместо потока; иначе — поштучно.
    Возвращает True, если ВСЁ доставлено (можно фиксировать состояние).
    can_send=False (каналы не настроены / пауза) → не шлём и не фиксируем.
    parse_mode прокидывается в send_alert (дайджест-обёртка html-безопасна)."""
    if not messages:
        return True
    if not can_send:
        return False
    # тихие часы/выключенные каналы уже отсеяны выше (can_send) — персональные
    # получатели подчиняются тем же правилам, отдельного «ночью можно» у них нет
    if session_factory is not None:
        await send_personal(session_factory, cfg, messages, threshold, parse_mode)
    if threshold and len(messages) >= threshold:
        return not await send_alert(cfg, _digest(messages), parse_mode)
    ok = True
    for m in messages:
        if await send_alert(cfg, m, parse_mode):
            ok = False  # хотя бы один канал не принял — повторим на следующем цикле
    return ok


async def security_alert(session: AsyncSession, settings: Settings, text: str) -> None:
    """Шлёт security-событие (брутфорс, смена пароля и т.п.) во все настроенные
    каналы. Best-effort — не роняет вызывающую операцию."""
    try:
        cfg = await settings_store.get_alert_config(session, settings)
        if alerts_enabled(cfg):
            await send_alert(cfg, text)
    except Exception:  # noqa: BLE001
        log.warning("security-алерт не отправлен", exc_info=True)
