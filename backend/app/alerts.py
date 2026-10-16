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
from app.models import AlertEvent, Check, Server, TgAlert, User

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


# Шторм: один и тот же сбой сразу на нескольких серверах. 13.10 на шести серверах каждой панели
# разом упал один и тот же сервис, и каждый сервер прислал свое сообщение. С STORM_MIN серверов
# за раз уходит одно сообщение со списком. Склеиваются только одинаковые: тот же вид, раздел и
# группа (маршруты по группам не ломаются), тот же текст без имени сервера и "N мин назад".
# Разные числа (диск 91% и 97%) - разные сообщения. Заметка о сервере делает текст другим, и
# такой сервер идет своим сообщением.
STORM_MIN = 3
_STORM_NAMES = 30
_AGO = re.compile(r"\d+\s*(?:с|сек|мин|ч|дн|д)\.?\s+назад")
_HREF = re.compile(r'<a href="[^"]*">')
# "это мы" и остальное, где кнопка про один сервер
_STORM_SKIP = frozenset({"access_change"})


def _servers_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "сервер"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "сервера"
    return "серверов"


def _storm_key(m) -> tuple | None:
    if not isinstance(m, Msg) or not m.target or m.kind in _STORM_SKIP:
        return None
    if m.ref and not m.ref.startswith("srv:"):
        return None  # мониторы сайтов склеиваются своим путем, по доменам
    if not m.ref and not (m.recovery and m.section in _SERVER_SECTIONS):
        return None
    nm = html.escape(m.target)
    text = str(m)
    if nm not in text:
        return None  # свой шаблон без имени сервера: не понять, что у них общего
    norm = _AGO.sub("#назад", _HREF.sub("<a>", text.replace(nm, "\x00")))
    return (m.kind, m.section, m.group, m.recovery, norm)


def _linked(m: Msg) -> str:
    nm = html.escape(m.target)
    hit = re.search(r'<a href="[^"]*">' + re.escape(nm) + "</a>", str(m))
    return hit.group(0) if hit else nm


def _storm_msg(parts: list) -> Msg:
    first = parts[0]
    names = list(dict.fromkeys(p.target for p in parts))
    links = list(dict.fromkeys(_linked(p) for p in parts))
    shown = links[:_STORM_NAMES]
    more = f" и еще {len(links) - len(shown)}" if len(links) > len(shown) else ""
    head, _, tail = str(first).partition("\n")
    label = f"<b>{len(names)} {_servers_word(len(names))}</b>"
    own = _linked(first)
    head = head.replace(own, label, 1) if own in head else head
    body = head + "\n↳ " + ", ".join(shown) + more + (f"\n{tail}" if tail else "")
    ids = [x for p in parts for x in str(p.ref or "").partition(":")[2].split(",") if x.strip().isdigit()]
    ref = "srv:" + ",".join(dict.fromkeys(ids)) if ids else ""
    out = Msg(body, first.section, first.group, first.kind, ", ".join(names), first.recovery, ref)
    out.parts = list(parts)  # история пишется по каждому серверу
    return out


def merge_storm(messages: list) -> list:
    """Одинаковые сообщения с STORM_MIN и больше серверов - одним, на месте первого."""
    groups: dict[tuple, list[int]] = {}
    for i, m in enumerate(messages):
        k = _storm_key(m)
        if k is not None:
            groups.setdefault(k, []).append(i)
    big = {idx[0]: idx for idx in groups.values()
           if len({messages[i].target for i in idx}) >= STORM_MIN}
    if not big:
        return messages
    drop = {i for idx in big.values() for i in idx[1:]}
    return [_storm_msg([messages[j] for j in big[i]]) if i in big else m
            for i, m in enumerate(messages) if i not in drop]


def route_hits(cfg: dict, m: str) -> list[dict]:
    """Чаты по группам, которым положен этот алерт: группа объекта есть в списке чата. Алерт без
    группы (общий, про саму панель, сервер вне групп) в чаты по группам не идет."""
    section, group = _msg_scope(m)
    if not section or not group:
        return []
    field = "server_groups" if section in _SERVER_SECTIONS else "site_groups"
    return [r for r in (cfg.get("routes") or {}).get("chats") or [] if group in r.get(field, [])]


def chats_for(cfg: dict, m: str) -> list[str]:
    """Куда в Telegram идет алерт: основной чат (всё или только то, что не забрали чаты по
    группам) и чаты по группам. Без повторов, основной первым."""
    main = str(cfg.get("telegram_chat") or "") if cfg.get("telegram_token") else ""
    if not cfg.get("telegram_token"):
        return []
    hits = [r["chat"] for r in route_hits(cfg, m)]
    out: list[str] = []
    if main and ((cfg.get("routes") or {}).get("main") != "rest" or not hits):
        out.append(main)
    for c in hits:
        if c not in out:
            out.append(c)
    return out


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


_PRE = re.compile(r"\n[^\n]*\n<pre>.*?</pre>", re.S)


def _digest(messages: list[str]) -> str:
    # хвост лога у алерта о Job в дайджест не берем: в пачке он не читается и раздувает
    # сообщение за предел Telegram
    head = [_PRE.sub("", m) for m in messages[:_DIGEST_LINES]]
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
    pairs = {(getattr(p, "kind", ""), getattr(p, "target", "")) for m in messages
             if getattr(m, "recovery", False) and getattr(m, "kind", "")
             for p in getattr(m, "parts", None) or [m]}
    if not pairs:
        return
    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            rows = (await session.scalars(select(TgAlert).where(
                TgAlert.resolved_at.is_(None), TgAlert.kind.in_({k for k, _ in pairs})))).all()
            rows = [r for r in rows if (r.kind, r.target) in pairs or await _storm_back(session, r, pairs)]
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


async def _storm_back(session, row: TgAlert, pairs: set) -> bool:
    """Алерт про несколько серверов сразу решен, когда вернулись все: отбой в этой пачке или
    уже записанный в состоянии сервера (отбои приходят в разные тики)."""
    obj, ids = _ref_ids(row.ref)
    if obj != "srv" or len(ids) < 2:
        return False
    for i in ids:
        s = await session.get(Server, i)
        if s is None or (row.kind, s.name) in pairs:
            continue
        lvl = (s.alert_state or {}).get(row.kind)
        if not isinstance(lvl, int) or lvl > 0:
            return False
    return True


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
    if session_factory is not None:
        messages = await with_notes(session_factory, messages)
    messages = merge_storm(messages)
    # кнопки под алертами: запись нужна до отправки, ее id едет в кнопках
    rows: dict[int, int] = {}
    if session_factory is not None and buttons_on(cfg):
        rows = await tg_buttons.prepare(session_factory, messages)
    sent: dict[int, list] = {}
    # тихие часы/выключенные каналы уже отсеяны выше (can_send) — персональные
    # получатели подчиняются тем же правилам, отдельного «ночью можно» у них нет
    if session_factory is not None:
        await send_personal(session_factory, cfg, messages, threshold, parse_mode, rows, sent)
    main = str(cfg.get("telegram_chat") or "") if cfg.get("telegram_token") else ""
    on_main = [bool(main) and main in chats_for(cfg, m) for m in messages]
    everything = list(range(len(messages)))
    if not main or all(on_main):
        ok, got = await _send_common(cfg, everything, messages, threshold, parse_mode, rows, sent)
    else:
        # основной чат получает только остальное, а вебхук - все, как и раньше
        ok, got = await _send_common({**cfg, "webhook": ""}, [i for i in everything if on_main[i]],
                                     messages, threshold, parse_mode, rows, sent)
        if cfg.get("webhook"):
            ok2, got2 = await _send_common({**cfg, "telegram_chat": ""}, everything, messages,
                                           threshold, parse_mode, {}, sent)
            ok, got = ok and ok2, got | got2
    if cfg.get("telegram_token"):
        rok, rgot = await _send_routes(cfg, messages, threshold, parse_mode, rows, sent,
                                       session_factory)
        ok, got = ok and rok, got | rgot
    delivered = [m for i, m in enumerate(messages) if i in got]
    if rows:
        await tg_buttons.store_sent(session_factory, sent)
    if session_factory is not None and buttons_on(cfg):
        await tg_resolve(session_factory, cfg, messages)
    if session_factory is not None and delivered:
        await record_history(session_factory, delivered)
    return ok


_NOTE_MAX = 300


def _ref_ids(ref: str) -> tuple[str, list[int]]:
    """"srv:5" -> ("srv", [5]), "chk:3,4" -> ("chk", [3, 4]); мусор -> ("", [])."""
    kind, _, rest = (ref or "").partition(":")
    ids = [int(x) for x in rest.split(",") if x.strip().isdigit()]
    return (kind, ids) if kind in ("srv", "chk") and ids else ("", [])


async def with_notes(session_factory, messages: list[str]) -> list[str]:
    """Дописать к алерту заметку о сервере или мониторе ("диск под замену, тикет у хостера"):
    дежурный видит контекст прямо в чате, не открывая панель. Отбою заметка не нужна, а
    сбой чтения не должен мешать отправке - тогда уходит как есть."""
    want: dict[str, set[int]] = {"srv": set(), "chk": set()}
    for m in messages:
        if getattr(m, "recovery", False):
            continue
        kind, ids = _ref_ids(getattr(m, "ref", ""))
        if kind:
            want[kind].update(ids)
    if not want["srv"] and not want["chk"]:
        return messages
    notes: dict[tuple[str, int], str] = {}
    try:
        async with session_factory() as session:
            for kind, model in (("srv", Server), ("chk", Check)):
                if want[kind]:
                    rows = await session.execute(
                        select(model.id, model.note).where(model.id.in_(want[kind]), model.note != ""))
                    notes.update({(kind, i): n for i, n in rows.all()})
    except Exception:  # noqa: BLE001
        log.warning("alert notes: read failed", exc_info=True)
        return messages
    if not notes:
        return messages
    out: list[str] = []
    for m in messages:
        kind, ids = _ref_ids(getattr(m, "ref", ""))
        got = [] if getattr(m, "recovery", False) else list(dict.fromkeys(
            notes[(kind, i)] for i in ids if (kind, i) in notes))
        if not got:
            out.append(m)
            continue
        text = " · ".join(" ".join(n.split()) for n in got)
        if len(text) > _NOTE_MAX:
            text = text[:_NOTE_MAX - 3].rstrip() + "..."
        out.append(Msg(f"{m}\n📝 <i>{html.escape(text)}</i>", m.section, m.group, m.kind,
                       m.target, m.recovery, m.ref))
    return out


def _channel_count(cfg: dict) -> int:
    """Сколько общих каналов настроено: алерт считается полученным, если принял хоть один."""
    return int(bool(cfg.get("telegram_token") and cfg.get("telegram_chat"))) + int(bool(cfg.get("webhook")))


async def _send_common(cfg: dict, idx: list[int], messages: list[str], threshold: int,
                       parse_mode: str | None, rows: dict[int, int], sent: dict[int, list]
                       ) -> tuple[bool, set[int]]:
    """Основной чат и вебхук: дайджестом, если сообщений много, иначе по одному. Хотя бы один
    канал не принял - повтор на следующем цикле. Возвращает, все ли ушло, и что дошло хоть куда."""
    channels = _channel_count(cfg)
    if not idx or not channels:
        return True, set()
    if threshold and len(idx) >= threshold:
        errors = await send_alert(cfg, _digest([messages[i] for i in idx]), parse_mode)
        return not errors, (set(idx) if len(errors) < channels else set())
    ok, got = True, set()
    for i in idx:
        m = messages[i]
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
            got.add(i)
    return ok, got


async def _send_batch(cfg: dict, chat: str, items: list[tuple[int, str]], threshold: int,
                      parse_mode: str | None, rows: dict[int, int], sent: dict[int, list]
                      ) -> tuple[list[int], str]:
    """Пачка сообщений в чат по группам: дайджестом, если их много, иначе по одному с кнопками.
    Возвращает, какие не ушли, и текст последней ошибки."""
    api, token = cfg.get("telegram_api", ""), cfg["telegram_token"]
    if threshold and len(items) >= threshold:
        try:
            await tg_send(api, token, chat, _digest([t for _, t in items]), parse_mode)
            return [], ""
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram-алерт в чат %s не отправлен: %s", chat, exc)
            return [i for i, _ in items], str(exc)
    failed: list[int] = []
    err = ""
    for i, text in items:
        aid = rows.get(i)
        markup = tg_buttons.keyboard(aid, getattr(text, "kind", "")) if aid else None
        try:
            mid = await tg_send(api, token, chat, text, parse_mode, markup)
            if aid and mid is not None:
                sent.setdefault(aid, []).append({"chat": chat, "id": mid})
        except Exception as exc:  # noqa: BLE001 — алерт не должен ронять цикл
            log.warning("Telegram-алерт в чат %s не отправлен: %s", chat, exc)
            failed.append(i)
            err = str(exc)
    return failed, err


def _fallback_text(m: str, name: str, err: str, parse_mode: str | None) -> str:
    """Сообщение, которое чат по группам не принял, в основном чате: с припиской, почему оно
    здесь - иначе непонятно, откуда в основном чате алерты чужой группы."""
    note = f"не дошло в чат «{name}»: {err[:150]}"
    return f"{m}\n↪ {html.escape(note) if parse_mode == 'HTML' else note}"


async def _send_routes(cfg: dict, messages: list[str], threshold: int, parse_mode: str | None,
                       rows: dict[int, int], sent: dict[int, list], session_factory=None
                       ) -> tuple[bool, set[int]]:
    """Чаты по группам. Чат, который не принял сообщение, повтора не вызывает (иначе основной
    чат получал бы тот же алерт каждый цикл): сообщение уходит в основной чат с припиской, если
    тот его еще не получил, а ошибка видна в настройках."""
    main = str(cfg.get("telegram_chat") or "")
    names = {r["chat"]: r.get("name") or r["chat"] for r in (cfg.get("routes") or {}).get("chats") or []}
    plan: dict[str, list[tuple[int, str]]] = {}
    for i, m in enumerate(messages):
        for chat in chats_for(cfg, m):
            if chat != main:
                plan.setdefault(chat, []).append((i, m))
    if not plan:
        return True, set()
    got: set[int] = set()
    errors: dict[str, str] = {}
    fallback: list[str] = []
    back: list[int] = []
    for chat, items in plan.items():
        failed, err = await _send_batch(cfg, chat, items, threshold, parse_mode, rows, sent)
        got.update(i for i, _ in items if i not in failed)
        if failed:
            errors[chat] = err
        for i in failed:
            if main and main not in chats_for(cfg, messages[i]) and i not in back:
                back.append(i)
                fallback.append(_fallback_text(messages[i], names.get(chat, chat), err, parse_mode))
    ok = True
    if fallback:
        fok, fgot = await _send_common({**cfg, "webhook": ""}, list(range(len(fallback))), fallback,
                                       threshold, parse_mode, {}, sent)
        ok = fok
        got.update(back[j] for j in fgot)
    if session_factory is not None:
        await _note_routes(session_factory, set(plan), errors)
    return ok, got


async def _note_routes(session_factory, used: set[str], errors: dict[str, str]) -> None:
    """Запомнить, какой чат по группам не принял сообщение (и забыть, когда снова принял)."""
    try:
        async with session_factory() as session:
            state = await settings_store.get_routes_state(session)
            new = {c: v for c, v in state.items() if c not in used or c in errors}
            now = datetime.now(timezone.utc).isoformat()
            for c, e in errors.items():
                new[c] = {"err": e[:300], "at": now}
            if new != state:
                await settings_store.set_routes_state(session, new)
                await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("состояние чатов по группам не записано", exc_info=True)


_TAG = re.compile(r"<[^>]+>")


async def record_history(session_factory, messages: list[str]) -> None:
    """Записать доставленные алерты в историю (AlertEvent). Текст без HTML-разметки, как его
    видит человек. Сбой записи не должен ни ронять отправку, ни вызывать повтор: алерт уже
    ушел, и вторая копия в чате хуже пропуска в истории."""
    try:
        async with session_factory() as session:
            for m in [p for x in messages for p in getattr(x, "parts", None) or [x]]:
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
