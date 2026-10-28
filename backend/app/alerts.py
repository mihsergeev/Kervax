"""Алерты (Telegram / вебхук): отправка в каналы и security-события.

Пороговые алерты по мониторам/инцидентам добавляются в Этапе 3 — здесь базовый
транспорт (send_alert) и security-события (брутфорс, смена пароля и т.п.).
"""

import contextvars
import html
import logging
import re

from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store, tg_buttons, tg_fmt
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
    # Короткий формат (app/tg_fmt.py): в чат уходит сам текст, а full - полный, с подробностями,
    # для истории алертов и панели; line - этот же алерт одной строкой для пачки. Пусто - старый
    # формат, где текст и есть полный
    full: str
    line: str
    # точные ключи мьюта, которые пишет кнопка "заглушить" под алертом вместо вида целиком;
    # пусто - глушится вид
    mutes: list[str]

    def __new__(cls, text: str, section: str = "", group: str = "", kind: str = "",
                target: str = "", recovery: bool = False, ref: str = "", full: str = "",
                line: str = "", mutes: list[str] | None = None) -> "Msg":
        obj = super().__new__(cls, text)
        obj.section = section
        obj.group = group
        obj.kind = kind
        obj.target = target
        obj.recovery = recovery
        obj.ref = ref
        obj.full = full
        obj.line = line
        obj.mutes = list(mutes or [])
        return obj

    def tail(self, extra: str, short: str = "") -> "Msg":
        """Дописать хвост (причину, что менялось до сбоя, лог). У короткого алерта хвост идет
        только в полный текст, в чат - short, если он есть: подробности смотрят в панели."""
        if self.full:
            text = str(self) + (short if short else "")
            out = Msg(text, self.section, self.group, self.kind, self.target, self.recovery, self.ref,
                      self.full + extra, self.line, mutes=self.mutes)
        else:
            out = Msg(str(self) + extra, self.section, self.group, self.kind, self.target, self.recovery,
                      self.ref, mutes=self.mutes)
        if getattr(self, "parts", None):
            out.parts = self.parts
        # куда ушло сообщение (app/site_group.py его потом правит): список общий с исходным
        if getattr(self, "sent_to", None) is not None:
            out.sent_to = self.sent_to
        return out


def full_text(m: str) -> str:
    """Полный текст алерта (с подробностями), как он лежит в истории: у короткого - full."""
    return getattr(m, "full", "") or str(m)


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
_STORM_SHORT = 6  # серверов ссылками в коротком сообщении, остальные - "и еще N"
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
    label = f"<b>{len(names)} {_servers_word(len(names))}</b>"
    own = _linked(first)
    full = ""
    line = ""
    if getattr(first, "full", ""):
        # короткий формат: "🔴⚙️ 6 серверов", что случилось, конкретика, последней строкой -
        # какие серверы (ссылками). Полный список - в полном тексте
        shown = links[:_STORM_SHORT]
        more = f" и еще {len(links) - len(shown)}" if len(links) > len(shown) else ""
        head, _, tail = str(first).partition("\n")
        head = head.replace(own, label, 1) if own in head else head
        body = head + (f"\n{tail}" if tail else "") + "\n" + ", ".join(shown) + more
        f_head, _, f_tail = first.full.partition("\n")
        f_head = f_head.replace(own, label, 1) if own in f_head else f_head
        f_more = f" и еще {len(links) - _STORM_NAMES}" if len(links) > _STORM_NAMES else ""
        full = f_head + "\n↳ " + ", ".join(links[:_STORM_NAMES]) + f_more + (f"\n{f_tail}" if f_tail else "")
        line = first.line.replace(own, label, 1) if first.line and own in first.line else first.line
    else:
        shown = links[:_STORM_NAMES]
        more = f" и еще {len(links) - len(shown)}" if len(links) > len(shown) else ""
        head, _, tail = str(first).partition("\n")
        head = head.replace(own, label, 1) if own in head else head
        body = head + "\n" + ", ".join(shown) + more + (f"\n{tail}" if tail else "")
    ids = [x for p in parts for x in str(p.ref or "").partition(":")[2].split(",") if x.strip().isdigit()]
    ref = "srv:" + ",".join(dict.fromkeys(ids)) if ids else ""
    mutes = list(dict.fromkeys(k for p in parts for k in getattr(p, "mutes", None) or []))
    out = Msg(body, first.section, first.group, first.kind, ", ".join(names), first.recovery, ref, full, line,
              mutes=mutes)
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


# Telegram не принимает сообщение длиннее 4096 символов (400 Bad Request), а 400 на одном
# алерте раньше означал повтор всей пачки каждый тик. Запас - под "..." и закрывающие теги
TG_MAX = 4000
# connect короткий: недоступный api.telegram.org должен давать отказ за секунды, а не держать
# тик минутами; read длиннее - Telegram бывает медленным, но отвечает
TG_TIMEOUT = httpx.Timeout(10, connect=4)

_TAG = re.compile(r"<[^>]+>")
_OPEN_CLOSE = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")


class TgRejected(Exception):
    """Telegram отказал навсегда (4xx кроме 429: битая разметка после повтора обычным текстом,
    чат не найден, бот удален из чата): тот же запрос на следующем тике получит тот же отказ."""


class TgDown(Exception):
    """Telegram или вебхук уже не ответил в этой отправке пачки: остальное туда не шлем,
    повтор на следующем тике."""


def _u16(s: str) -> int:
    """Длина так, как ее считает Telegram: в UTF-16 (эмодзи - два)."""
    return len(s.encode("utf-16-le")) // 2


def _plain(text: str) -> str:
    """HTML Telegram -> обычный текст: теги сняты, сущности раскодированы."""
    return html.unescape(_TAG.sub("", str(text)))


def _clip(text: str, html_mode: bool, limit: int = TG_MAX) -> str:
    """Обрезать до limit: по последнему переносу строки, если он не слишком рано, и не посреди
    тега или сущности (&amp;). В HTML незакрытые теги закрываются, иначе Telegram не разберет
    разметку."""
    text = str(text)
    if _u16(text) <= limit:
        return text
    room = limit - 3  # "..."
    n = room
    while n > 0 and _u16(text[:n]) > room:
        n -= _u16(text[:n]) - room
    nl = text.rfind("\n", 0, n)
    if nl > n // 2:
        n = nl
    if html_mode:
        lt = text.rfind("<", 0, n)
        if lt > text.rfind(">", 0, n):
            n = lt  # посреди тега
        amp = text.rfind("&", 0, n)
        if amp > text.rfind(";", 0, n):
            n = amp  # посреди &amp; (в HTML-тексте любой & экранирован, значит это обрезок)
    head = text[:n].rstrip() + "..."
    if not html_mode:
        return head
    stack: list[str] = []
    for close, tag in _OPEN_CLOSE.findall(head):
        tag = tag.lower()
        if not close:
            stack.append(tag)
        elif tag in stack:
            while stack and stack.pop() != tag:
                pass
    return head + "".join(f"</{t}>" for t in reversed(stack))


def _status(r) -> int:
    code = getattr(r, "status_code", 200)
    return code if isinstance(code, int) else 200


def _tg_why(r) -> str:
    """Описание отказа от Telegram ("Bad Request: can't parse entities ...")."""
    try:
        return str(r.json().get("description") or "без описания")[:200]
    except Exception:  # noqa: BLE001
        return "без описания"


# Отказы Telegram, которые человек чинит сам: голое "HTTP 400: Bad Request: chat not found" не говорит,
# что делать (типично после смены токена: новый бот еще не в чатах). Подстрока описания -> что сказать
_TG_HINTS = (
    ("chat not found", "бот не состоит в чате {chat}: добавьте его в группу, а в личке откройте бота и нажмите Start"),
    ("bot was blocked by the user", "в чате {chat} бота заблокировали: разблокируйте его и нажмите Start"),
    ("bot can't initiate conversation", "бот не может писать первым в {chat}: откройте бота и нажмите Start"),
    ("bot was kicked", "бота удалили из чата {chat}: добавьте его обратно"),
    ("bot is not a member", "бот не участник чата {chat}: добавьте его туда"),
    ("need administrator rights", "в канале {chat} бот должен быть админом с правом публиковать"),
    ("not enough rights", "в чате {chat} боту запрещено писать: разрешите ему отправку сообщений"),
    ("upgraded to a supergroup", "группа {chat} стала супергруппой, у нее новый id{new}: впишите его в чаты"),
    ("unauthorized", "токен бота неверный или отозван: возьмите текущий у @BotFather"),
    ("not found", "Telegram не узнал бота: проверьте токен и адрес API"),
)


def _tg_hint(r, chat: str) -> str:
    """Что не так и что делать по отказу Telegram, '' - отказ незнакомый."""
    why = _tg_why(r).lower()
    new = ""
    with suppress(Exception):
        new = str((r.json().get("parameters") or {}).get("migrate_to_chat_id") or "")
    for part, hint in _TG_HINTS:
        if part in why:
            return hint.format(chat=chat, new=f" {new}" if new else "")
    return ""


def _tg_error(r, code: int, chat: str) -> str:
    raw = f"HTTP {code}: {_tg_why(r)}"
    hint = _tg_hint(r, chat)
    return f"{hint} ({raw})" if hint else raw


async def _post(http, url: str, payload: dict):
    if http is not None:
        return await http.post(url, json=payload)
    async with httpx.AsyncClient(timeout=TG_TIMEOUT) as own:
        return await own.post(url, json=payload)


async def tg_send(
    api: str, token: str, chat: str, text: str, parse_mode: str | None = None,
    reply_markup: dict | None = None, http=None,
) -> int | None:
    """Одно сообщение в Telegram. Бросает исключение - обработка на вызывающем.
    Возвращает message_id (нужен, чтобы потом дописать в сообщение "взял" или "заглушено").

    Длинный текст режется до TG_MAX. Telegram не принял сообщение с разметкой (400) - еще раз
    обычным текстом, кнопки те же. Снова 4xx (кроме 429) - TgRejected: повтор не поможет.
    Сеть, таймаут, 5xx и 429 - обычное исключение, повтор на следующем тике.
    В тексте ошибки нет адреса запроса: в нем токен бота, а ошибка видна в настройках и логах.
    http - общий клиент на всю пачку (dispatch); без него - свой клиент на одно сообщение."""
    base = (api or "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendMessage"
    html_mode = (parse_mode or "").upper() == "HTML"
    payload = {"chat_id": chat, "text": _clip(text, html_mode), "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = await _post(http, url, payload)
    code = _status(r)
    if code == 400 and parse_mode:
        log.warning("Telegram не принял сообщение в %s (%s), шлем обычным текстом", chat, _tg_why(r))
        payload.pop("parse_mode")
        payload["text"] = _clip(_plain(text) if html_mode else str(text), False)
        r = await _post(http, url, payload)
        code = _status(r)
    if 400 <= code < 500 and code != 429:
        raise TgRejected(_tg_error(r, code, str(chat)))
    if code >= 400:
        raise RuntimeError(f"HTTP {code}: {_tg_why(r)}")
    r.raise_for_status()
    try:
        return int(r.json()["result"]["message_id"])
    except Exception:  # noqa: BLE001 - без id сообщение просто не поправить потом
        return None


# Вебхук (Mattermost, Discord, Slack) читает разметку markdown: "[текст](url)" из имени
# контейнера становился ссылкой, "@everyone" и "<@id>" - упоминанием всех. Скобки экранируются
# обратным слэшем (его понимают Mattermost и Discord), сам слэш - тоже, иначе "\[" в имени снимал
# бы экранирование. "@" - с пробелом нулевой ширины после (U+200B): обратный слэш перед "@"
# понимают не все получатели, а "@" + U+200B не разбирается как упоминание, и текст выглядит так же
_HOOK_MD = re.compile(r"([\\\[\]()<>])")


def webhook_text(text: str, parse_mode: str | None = None) -> str:
    """Текст для вебхука: без HTML Telegram и без разметки, которую понял бы получатель."""
    s = _plain(text) if (parse_mode or "").upper() == "HTML" else str(text)
    return _HOOK_MD.sub(r"\\\1", s).replace("@", "@\u200b")


class _Link:
    """Связь на одну отправку пачки (dispatch): один http-клиент на все сообщения и память о
    том, что Telegram или вебхук не отвечают. Без нее при недоступном api.telegram.org каждое
    сообщение ждало свой таймаут, и тик растягивался на минуты."""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.http = None
        self.tg_down = ""
        self.hook_down = ""
        self.hook_err: str | None = None  # None - вебхук не звали, "" - принял, иначе ошибка

    async def client(self):
        if self.http is None:
            self._stack = AsyncExitStack()
            self.http = await self._stack.enter_async_context(httpx.AsyncClient(timeout=TG_TIMEOUT))
        return self.http

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                log.warning("http-клиент алертов не закрыт", exc_info=True)
        self._stack, self.http = None, None

    async def tg(self, api: str, token: str, chat: str, text: str, parse_mode: str | None = None,
                 reply_markup: dict | None = None) -> int | None:
        if self.tg_down:
            raise TgDown(self.tg_down)
        try:
            return await tg_send(api, token, chat, text, parse_mode, reply_markup, http=await self.client())
        except httpx.TransportError as exc:  # таймаут, коннект: остальным сообщениям то же самое
            self.tg_down = f"Telegram не отвечает ({type(exc).__name__}), остальное в этот раз не слали"
            raise

    async def hook(self, url: str, text: str, parse_mode: str | None) -> str:
        """Одно сообщение в вебхук. Возвращает текст ошибки, пусто - принял."""
        if self.hook_down:
            err = self.hook_down
        else:
            try:
                r = await (await self.client()).post(url, json={"text": webhook_text(text, parse_mode)})
                code = _status(r)
                # без адреса: в адресе вебхука бывает секрет (у Discord токен прямо в пути)
                if code >= 400:
                    raise RuntimeError(f"HTTP {code}")
                r.raise_for_status()
                if self.hook_err is None:
                    self.hook_err = ""
                return ""
            except httpx.TransportError as exc:
                self.hook_down = err = f"не отвечает ({type(exc).__name__})"
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:300]
        self.hook_err = err
        return err


_LINK: contextvars.ContextVar[_Link | None] = contextvars.ContextVar("kervax_alert_link", default=None)


@asynccontextmanager
async def _link():
    """Связь текущей отправки пачки, а вне dispatch - своя на один вызов. Через contextvar, а
    не параметром: send_alert подменяют в десятках тестов с сигнатурой (cfg, text, parse_mode)."""
    cur = _LINK.get()
    if cur is not None:
        yield cur
        return
    own = _Link()
    token = _LINK.set(own)
    try:
        yield own
    finally:
        _LINK.reset(token)
        await own.close()


class SendErrors(list):
    """Ошибки send_alert - список строк, как раньше. rejected - сколько из них отказ навсегда
    (TgRejected): такое сообщение повторять незачем."""

    rejected: int = 0


async def send_alert(cfg: dict, text: str, parse_mode: str | None = None,
                     reply_markup: dict | None = None, sent: list | None = None) -> list[str]:
    """Шлёт текст во все настроенные каналы. Возвращает список ошибок (пустой = ок).
    parse_mode="HTML" — для сайтовых алертов со ссылкой <a href> в тексте (тогда
    весь динамический контент обязан быть html-экранирован вызывающим).
    reply_markup - кнопки под сообщением в Telegram; sent - сюда дописывается, куда ушло.
    Список - SendErrors: в rejected видно, сколько ошибок не исправит повтор."""
    errors = SendErrors()
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
    async with _link() as link:
        if token and chat:
            try:
                mid = await link.tg(cfg.get("telegram_api", ""), token, chat, text, parse_mode,
                                    reply_markup)
                if sent is not None and mid is not None:
                    sent.append({"chat": str(chat), "id": mid})
            except TgRejected as exc:
                errors.append(f"Telegram: {exc}")
                errors.rejected += 1
                log.warning("Telegram отказал в алерте: %s", exc)
            except Exception as exc:  # noqa: BLE001 — алерт не должен ронять цикл
                errors.append(f"Telegram: {exc}")
                log.warning("Telegram-алерт не отправлен: %s", exc)

        if cfg.get("webhook"):
            err = await link.hook(cfg["webhook"], text, parse_mode)
            if err:
                errors.append(f"Webhook: {err}")
                log.warning("Вебхук-алерт не отправлен: %s", err)
    return errors


_DIGEST_LINES = 20  # сколько алертов показать в пачке (остальные - счетчиком)


_PRE = re.compile(r"\n[^\n]*\n<pre>.*?</pre>", re.S)


def _digest_line(m: str) -> str:
    """Алерт в пачке - одной строкой: готовая line у короткого формата, иначе первая строка
    текста (хвост лога у Job и подробности в пачке не читаются и раздувают сообщение)."""
    line = getattr(m, "line", "")
    if line:
        return line
    return tg_fmt.clean(_PRE.sub("", str(m))).split("\n")[0]


def _digest(messages: list[str]) -> str:
    head = [_digest_line(m) for m in messages[:_DIGEST_LINES]]
    extra = len(messages) - len(head)
    body = "\n".join(head)
    if extra:
        body += f"\nи еще {extra}"
    # без заголовка "N алертов за раз": счетчик ничего не говорит, строки и так видны
    return body


async def tg_edit(cfg: dict, chat: str, message_id: int, text: str, reply_markup: dict | None) -> None:
    """Поправить отправленное сообщение: дописать статус и сменить кнопки. Сбой не страшен."""
    base = (cfg.get("telegram_api") or "https://api.telegram.org").rstrip("/")
    try:
        async with _link() as link:
            if link.tg_down:
                return  # Telegram в этой пачке уже не ответил: правка подождет до следующего раза
            body = {"chat_id": chat, "message_id": message_id, "text": _clip(text, True), "parse_mode": "HTML",
                    "disable_web_page_preview": True}
            if reply_markup is not None:
                body["reply_markup"] = reply_markup
            try:
                await (await link.client()).post(f"{base}/bot{cfg['telegram_token']}/editMessageText", json=body)
            except httpx.TransportError as exc:
                link.tg_down = f"Telegram не отвечает ({type(exc).__name__}), остальное в этот раз не слали"
                raise
    except Exception:  # noqa: BLE001
        log.warning("сообщение %s/%s не поправлено", chat, message_id, exc_info=True)


async def tg_resolve(session_factory, cfg: dict, messages: list) -> None:
    """Пришел отбой: алерты с кнопками про то же (вид + объект) помечаем решенными и
    дописываем в них "снова в норме" - эскалировать и глушить больше нечего."""
    pairs = set()
    for m in messages:
        if not getattr(m, "recovery", False) or not getattr(m, "kind", ""):
            continue
        own = getattr(m, "resolves", None)  # свой список (отбой группы мониторов), [] - ничего
        pairs |= set(own) if own is not None else {
            (getattr(p, "kind", ""), getattr(p, "target", "")) for p in getattr(m, "parts", None) or [m]}
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
    async with _link() as link:
        await _send_personal(link, api, common, users, messages, threshold, parse_mode, rows, sent)


async def _send_personal(link: "_Link", api: str, common: str, users: list, messages: list[str],
                         threshold: int, parse_mode: str | None, rows: dict[int, int] | None,
                         sent: dict[int, list] | None) -> None:
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
                mid = await link.tg(api, token, user.tg_chat_id, text, parse_mode, markup)
                if aid and mid is not None and sent is not None:
                    sent.setdefault(aid, []).append({"chat": str(user.tg_chat_id), "id": mid})
                if mid is not None and not own_bot:
                    _note_sent(text, [{"chat": str(user.tg_chat_id), "id": mid}])
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
    Возвращает True, если повторять нечего (можно фиксировать состояние): каждое сообщение
    ушло хоть в один общий канал (Telegram или вебхук) или отклонено навсегда (TgRejected,
    в журнале ошибок). Умерший вебхук при живом Telegram повтора не вызывает, ошибка вебхука
    запоминается рядом с ошибками чатов по группам (ключ "_webhook").
    can_send=False (каналы не настроены / пауза) → не шлём и не фиксируем.
    parse_mode прокидывается в send_alert (дайджест-обёртка html-безопасна).
    Вся пачка идет через один http-клиент; после первого таймаута Telegram остальное в
    Telegram в этот раз не шлется (повтор на следующем тике)."""
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
    async with _link() as link:
        # тихие часы/выключенные каналы уже отсеяны выше (can_send) — персональные
        # получатели подчиняются тем же правилам, отдельного «ночью можно» у них нет
        if session_factory is not None:
            await send_personal(session_factory, cfg, messages, threshold, parse_mode, rows, sent)
        main = str(cfg.get("telegram_chat") or "") if cfg.get("telegram_token") else ""
        on_main = [bool(main) and main in chats_for(cfg, m) for m in messages]
        everything = list(range(len(messages)))
        if not main or all(on_main):
            got, again = await _send_common(cfg, everything, messages, threshold, parse_mode, rows, sent)
        else:
            # основной чат получает только остальное, а вебхук - все, как и раньше
            got, again = await _send_common({**cfg, "webhook": ""}, [i for i in everything if on_main[i]],
                                            messages, threshold, parse_mode, rows, sent)
            if cfg.get("webhook"):
                got2, again2 = await _send_common({**cfg, "telegram_chat": ""}, everything, messages,
                                                  threshold, parse_mode, {}, sent)
                got, again = got | got2, again | again2
        # ушло хоть в один общий канал - доставлено, сбой второго канала повтора не вызывает
        again -= got
        ok = True
        if cfg.get("telegram_token"):
            rok, rgot = await _send_routes(cfg, messages, threshold, parse_mode, rows, sent,
                                           session_factory)
            ok, got = rok, got | rgot
            # то, что основной чат не получает (main=rest), в Telegram идет чатом по группам: если
            # тот принял, а вебхук нет - сообщение доставлено, как с основным чатом. Сбой самого
            # основного чата чат по группам не покрывает: там повтор, как и раньше
            again -= {i for i in rgot if not on_main[i]}
        ok = ok and not again
        delivered = [m for i, m in enumerate(messages) if i in got]
        if rows:
            await tg_buttons.store_sent(session_factory, sent)
        if session_factory is not None and buttons_on(cfg):
            await tg_resolve(session_factory, cfg, messages)
        if session_factory is not None:
            await _note_webhook(session_factory, link.hook_err if cfg.get("webhook") else "")
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
        text = "; ".join(" ".join(n.split()) for n in got)
        if len(text) > _NOTE_MAX:
            text = text[:_NOTE_MAX - 3].rstrip() + "..."
        note = f"\n📝 <i>{html.escape(tg_fmt.clean(text))}</i>"
        if isinstance(m, Msg):
            # заметка - контекст для дежурного, она нужна и в коротком тексте
            out.append(m.tail(note, note))
        else:
            out.append(Msg(f"{m}{note}"))
    return out


def _note_sent(m, where: list) -> None:
    """Сообщение, которое потом поправят (sent_to), запоминает, куда ушло: чат и id в Telegram.
    Только через общий бот - править будет он."""
    lst = getattr(m, "sent_to", None)
    if lst is not None and where:
        lst.extend(x for x in where if x not in lst)


def _channel_count(cfg: dict) -> int:
    """Сколько общих каналов настроено: алерт считается полученным, если принял хоть один."""
    return int(bool(cfg.get("telegram_token") and cfg.get("telegram_chat"))) + int(bool(cfg.get("webhook")))


def _outcome(errors: list[str], channels: int, text: str) -> tuple[bool, bool]:
    """Итог send_alert для одного сообщения: дошло ли хоть куда и нужен ли повтор. Повтор - только
    если не дошло никуда и хоть один канал сбоил не навсегда (сеть, 5xx, 429). Отказ навсегда
    (TgRejected) - в журнал ошибок панели и без повтора: иначе одно битое сообщение каждый тик
    тянуло бы за собой повтор всей пачки."""
    rejected = getattr(errors, "rejected", 0)
    got = len(errors) < channels
    if rejected:
        log.error("алерт не принят Telegram, повтора не будет: %s; текст: %s", "; ".join(errors),
                  _plain(str(text)).split("\n")[0][:200])
    return got, not got and len(errors) > rejected


async def _send_common(cfg: dict, idx: list[int], messages: list[str], threshold: int,
                       parse_mode: str | None, rows: dict[int, int], sent: dict[int, list]
                       ) -> tuple[set[int], set[int]]:
    """Основной чат и вебхук: дайджестом, если сообщений много, иначе по одному. Возвращает,
    что дошло хоть в один канал, и что повторить на следующем цикле (см. _outcome)."""
    channels = _channel_count(cfg)
    if not idx or not channels:
        return set(), set()
    if threshold and len(idx) >= threshold:
        text = _digest([messages[i] for i in idx])
        errors = await send_alert(cfg, text, parse_mode)
        got, again = _outcome(errors, channels, text)
        return (set(idx) if got else set()), (set(idx) if again else set())
    got, again = set(), set()
    for i in idx:
        m = messages[i]
        aid = rows.get(i)
        out: list = []
        if aid or getattr(m, "sent_to", None) is not None:
            markup = tg_buttons.keyboard(aid, getattr(m, "kind", "")) if aid else None
            errors = await send_alert(cfg, m, parse_mode, markup, out)
        else:
            errors = await send_alert(cfg, m, parse_mode)
        if aid and out:
            sent.setdefault(aid, []).extend(out)
        _note_sent(m, out)
        g, a = _outcome(errors, channels, m)
        if g:
            got.add(i)
        if a:
            again.add(i)
    return got, again


async def _send_batch(cfg: dict, chat: str, items: list[tuple[int, str]], threshold: int,
                      parse_mode: str | None, rows: dict[int, int], sent: dict[int, list]
                      ) -> tuple[list[int], str]:
    """Пачка сообщений в чат по группам: дайджестом, если их много, иначе по одному с кнопками.
    Возвращает, какие не ушли, и текст последней ошибки."""
    async with _link() as link:
        return await _send_batch_via(link, cfg, chat, items, threshold, parse_mode, rows, sent)


async def _send_batch_via(link: "_Link", cfg: dict, chat: str, items: list[tuple[int, str]], threshold: int,
                          parse_mode: str | None, rows: dict[int, int], sent: dict[int, list]
                          ) -> tuple[list[int], str]:
    api, token = cfg.get("telegram_api", ""), cfg["telegram_token"]
    if threshold and len(items) >= threshold:
        try:
            await link.tg(api, token, chat, _digest([t for _, t in items]), parse_mode)
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
            mid = await link.tg(api, token, chat, text, parse_mode, markup)
            if aid and mid is not None:
                sent.setdefault(aid, []).append({"chat": chat, "id": mid})
            if mid is not None:
                _note_sent(text, [{"chat": chat, "id": mid}])
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
        fgot, fagain = await _send_common({**cfg, "webhook": ""}, list(range(len(fallback))), fallback,
                                          threshold, parse_mode, {}, sent)
        ok = not fagain
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


# ошибка вебхука лежит рядом с ошибками чатов по группам: id чата с "_" не начинается
WEBHOOK_STATE_KEY = "_webhook"


async def _note_webhook(session_factory, err: str | None) -> None:
    """Запомнить, что вебхук не принял алерт (повтора из-за него больше нет, и без этой записи
    сбой никто бы не увидел), и забыть, когда снова принял. err: None - вебхук в этой пачке не
    звали (состояние не трогаем), "" - принял или вебхук выключен, иначе текст ошибки."""
    if err is None:
        return
    try:
        async with session_factory() as session:
            state = await settings_store.get_routes_state(session)
            new = dict(state)
            if err:
                new[WEBHOOK_STATE_KEY] = {"err": err[:300], "at": datetime.now(timezone.utc).isoformat()}
            else:
                new.pop(WEBHOOK_STATE_KEY, None)
            if new != state:
                await settings_store.set_routes_state(session, new)
                await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("состояние вебхука не записано", exc_info=True)


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
                    text=html.unescape(_TAG.sub("", getattr(m, "full", "") or str(m)))[:4000],
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
