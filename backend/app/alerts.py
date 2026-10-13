"""Алерты (Telegram / вебхук): отправка в каналы и security-события.

Пороговые алерты по мониторам/инцидентам добавляются в Этапе 3 — здесь базовый
транспорт (send_alert) и security-события (брутфорс, смена пароля и т.п.).
"""

import html
import logging
import re

from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store, tg_buttons
from app.config import Settings
from app.models import AlertEvent, TgAlert, User

log = logging.getLogger("kervax.alerts")


class Msg(str):
    """Текст алерта + адресация: раздел панели и группа объекта.

    Наследник str сознательно: общий канал и весь существующий код работают с
    сообщением как со строкой, а персональная рассылка дополнительно читает
    section/group, чтобы понять, кого это касается. Так адресация появилась без
    переписывания трёх десятков мест, где алерты собираются."""

    section: str
    group: str
    # для истории алертов: вид, про какой сервер или монитор, отбой ли это
    kind: str
    target: str
    recovery: bool
    # что глушат кнопки под алертом в Telegram: "srv:<id>" или "chk:<id>[,<id>...]";
    # пусто - кнопок нет
    ref: str

    def __new__(cls, text: str, section: str = "", group: str = "", kind: str = "",
                target: str = "", recovery: bool = False, ref: str = "") -> "Msg":
        obj = super().__new__(cls, text)
        obj.section = section
        obj.group = group
        obj.kind = kind
        obj.target = target
        obj.recovery = recovery
        obj.ref = ref
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
    api: str, token: str, chat: str, text: str, parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Одно сообщение в Telegram. Бросает исключение — обработка на вызывающем.
    Возвращает message_id (нужен, чтобы потом дописать в сообщение "взял" или "заглушено")."""
    base = (api or "https://api.telegram.org").rstrip("/")
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.post(f"{base}/bot{token}/sendMessage", json=payload)
        r.raise_for_status()
    try:
        return int(r.json()["result"]["message_id"])
    except Exception:  # noqa: BLE001 - без id сообщение просто не поправить потом
        return None


async def send_alert(cfg: dict, text: str, parse_mode: str | None = None,
                     reply_markup: dict | None = None, sent: list | None = None) -> list[str]:
    """Шлёт текст во все настроенные каналы. Возвращает список ошибок (пустой = ок).
    parse_mode="HTML" — для сайтовых алертов со ссылкой <a href> в тексте (тогда
    весь динамический контент обязан быть html-экранирован вызывающим).
    reply_markup - кнопки под сообщением в Telegram; sent - сюда дописывается, куда ушло."""
    errors: list[str] = []
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
    if token and chat:
        try:
            mid = await tg_send(cfg.get("telegram_api", ""), token, chat, text, parse_mode,
                                reply_markup)
            if sent is not None and mid is not None:
                sent.append({"chat": str(chat), "id": mid})
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


async def tg_edit(cfg: dict, chat: str, message_id: int, text: str, reply_markup: dict) -> None:
    """Поправить отправленное сообщение: дописать статус и сменить кнопки. Сбой не страшен."""
    base = (cfg.get("telegram_api") or "https://api.telegram.org").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(f"{base}/bot{cfg['telegram_token']}/editMessageText", json={
                "chat_id": chat, "message_id": message_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True, "reply_markup": reply_markup,
            })
    except Exception:  # noqa: BLE001
        log.warning("сообщение %s/%s не поправлено", chat, message_id, exc_info=True)


async def tg_resolve(session_factory, cfg: dict, messages: list) -> None:
    """Пришел отбой: алерты с кнопками про то же (вид + объект) помечаем решенными и
    дописываем в них "снова в норме" - эскалировать и глушить больше нечего."""
    pairs = {(getattr(m, "kind", ""), getattr(m, "target", "")) for m in messages
             if getattr(m, "recovery", False) and getattr(m, "kind", "")}
    if not pairs:
        return
    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            rows = (await session.scalars(select(TgAlert).where(
                TgAlert.resolved_at.is_(None), TgAlert.kind.in_({k for k, _ in pairs})))).all()
            rows = [r for r in rows if (r.kind, r.target) in pairs]
            for r in rows:
                r.resolved_at = now
            await session.commit()
            tz = cfg.get("tz") or "UTC"
            for r in rows:
                text, markup = tg_buttons.row_text(r, tz, now), tg_buttons.row_keyboard(r)
                for m in r.messages or []:
                    await tg_edit(cfg, m.get("chat"), m.get("id"), text, markup)
    except Exception:  # noqa: BLE001
        log.warning("отбой не отмечен в алертах с кнопками", exc_info=True)


def buttons_on(cfg: dict) -> bool:
    """Кнопки под алертами: включены в настройках и есть общий бот (его панель и читает)."""
    return bool(cfg.get("tg_buttons") and cfg.get("telegram_token"))


async def send_personal(
    session_factory, cfg: dict, messages: list[str], threshold: int,
    parse_mode: str | None = None, rows: dict[int, int] | None = None,
    sent: dict[int, list] | None = None,
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
        mine = [i for i, m in enumerate(messages) if wants(user, m)]
        if not mine:
            continue
        # кнопки нажимаются в общем боте: у своего бота человека их некому прочитать
        own_bot = bool(user.tg_token) and user.tg_token != common
        digest = bool(threshold and len(mine) >= threshold)
        batch = [(None, _digest([messages[i] for i in mine]))] if digest else [(i, messages[i]) for i in mine]
        for i, text in batch:
            aid = (rows or {}).get(i) if i is not None and not own_bot else None
            markup = tg_buttons.keyboard(aid, getattr(text, "kind", "")) if aid else None
            try:
                mid = await tg_send(api, token, user.tg_chat_id, text, parse_mode, markup)
                if aid and mid is not None and sent is not None:
                    sent.setdefault(aid, []).append({"chat": str(user.tg_chat_id), "id": mid})
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
    # кнопки под алертами: запись нужна до отправки, ее id едет в кнопках
    rows: dict[int, int] = {}
    if session_factory is not None and buttons_on(cfg):
        rows = await tg_buttons.prepare(session_factory, messages)
    sent: dict[int, list] = {}
    # тихие часы/выключенные каналы уже отсеяны выше (can_send) — персональные
    # получатели подчиняются тем же правилам, отдельного «ночью можно» у них нет
    if session_factory is not None:
        await send_personal(session_factory, cfg, messages, threshold, parse_mode, rows, sent)
    channels = _channel_count(cfg)
    if threshold and len(messages) >= threshold:
        errors = await send_alert(cfg, _digest(messages), parse_mode)
        delivered = list(messages) if len(errors) < channels else []
        ok = not errors
    else:
        ok, delivered = True, []
        for i, m in enumerate(messages):
            aid = rows.get(i)
            out: list = []
            if aid:
                markup = tg_buttons.keyboard(aid, getattr(m, "kind", ""))
                errors = await send_alert(cfg, m, parse_mode, markup, out)
            else:
                errors = await send_alert(cfg, m, parse_mode)
            if aid and out:
                sent.setdefault(aid, []).extend(out)
            if errors:
                ok = False  # хотя бы один канал не принял - повторим на следующем цикле
            if len(errors) < channels:
                delivered.append(m)
    if rows:
        await tg_buttons.store_sent(session_factory, sent)
    if session_factory is not None and buttons_on(cfg):
        await tg_resolve(session_factory, cfg, messages)
    if session_factory is not None and delivered:
        await record_history(session_factory, delivered)
    return ok


def _channel_count(cfg: dict) -> int:
    """Сколько общих каналов настроено: алерт считается полученным, если принял хоть один."""
    return int(bool(cfg.get("telegram_token") and cfg.get("telegram_chat"))) + int(bool(cfg.get("webhook")))


_TAG = re.compile(r"<[^>]+>")


async def record_history(session_factory, messages: list[str]) -> None:
    """Записать доставленные алерты в историю (AlertEvent). Текст без HTML-разметки, как его
    видит человек. Сбой записи не должен ни ронять отправку, ни вызывать повтор: алерт уже
    ушел, и вторая копия в чате хуже пропуска в истории."""
    try:
        async with session_factory() as session:
            for m in messages:
                session.add(AlertEvent(
                    kind=str(getattr(m, "kind", "") or "")[:40],
                    target=str(getattr(m, "target", "") or "")[:255],
                    section=str(getattr(m, "section", "") or "")[:20],
                    grp=str(getattr(m, "group", "") or "")[:255],
                    recovery=bool(getattr(m, "recovery", False)),
                    text=html.unescape(_TAG.sub("", str(m)))[:4000],
                ))
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("история алертов не записана", exc_info=True)


async def security_alert(session: AsyncSession, settings: Settings, text: str) -> None:
    """Шлёт security-событие (брутфорс, смена пароля и т.п.) во все настроенные
    каналы. Best-effort — не роняет вызывающую операцию."""
    try:
        cfg = await settings_store.get_alert_config(session, settings)
        if alerts_enabled(cfg):
            await send_alert(cfg, text)
    except Exception:  # noqa: BLE001
        log.warning("security-алерт не отправлен", exc_info=True)
