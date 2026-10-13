"""Бот панели: нажатия кнопок под алертами и команды /status, /mutes.

Апдейты забираем long polling'ом (getUpdates), а не вебхуком: панели стоят за белым списком,
Telegram до них не достучится. Читать апдейты бота может только один потребитель: если этого
бота уже читает другая программа или вторая панель, Telegram отвечает 409 - тогда показываем
это в настройках и ждем, а не воюем за апдейты.

Кто вправе жать кнопки: всякий в чате, куда ушел алерт. Общий чат алертов и так видят только
свои, а в личный чат алерт уходит владельцу учетки. Если человек привязан к учетке панели и это
"только просмотр" - он может только "взял".
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from app import alerts, audit, settings_store, tg_buttons
from app.config import Settings
from app.models import Check, Server, TgAlert, User

log = logging.getLogger("kervax.tg_bot")

_POLL_TIMEOUT = 25        # long polling: Telegram держит запрос до 25 с, если апдейтов нет
_BACKOFF_ERR = 30         # сеть или прокси к Telegram не отвечает
_BACKOFF_CONFLICT = 300   # бота читает кто-то еще
_IDLE = 20                # кнопки выключены: раз в 20 с смотрим, не включили ли
_KEEP_DAYS = 30           # записи кнопок старше месяца не нужны: такие алерты никто не жмет
_STATE_EVERY_S = 60       # "последний опрос" в настройках обновляем не чаще раза в минуту
_CB = re.compile(r"kx:(\d+):(" + "|".join(tg_buttons.OPS) + r")")
_MAX_TEXT = 3900

HELP = (
    "Команды:\n"
    "/status - что сейчас горит\n"
    "/mutes - что заглушено\n\n"
    "Кнопки под алертом: заглушить этот вид алерта на час, до 9 утра или насовсем, "
    "\"взял\" - отметить, что занимаешься. Заглушенное возвращает кнопка \"Вернуть алерты\"."
)


def _tok_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


def _base(cfg: dict) -> str:
    return (cfg.get("telegram_api") or "https://api.telegram.org").rstrip("/")


async def _call(cfg: dict, method: str, payload: dict, timeout: float = 15) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as http:
        r = await http.post(f"{_base(cfg)}/bot{cfg['telegram_token']}/{method}", json=payload)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": f"HTTP {r.status_code}"}


# --- кто нажал ----------------------------------------------------------------------------

async def _who(session, frm: dict) -> tuple[str, User | None]:
    """Имя для строки "взял ..." и учетка панели, если человек к ней привязан (его личный чат
    в Telegram - это id пользователя)."""
    uid = str(frm.get("id") or "")
    user = None
    if uid:
        user = await session.scalar(select(User).where(User.tg_chat_id == uid))
    if user is not None:
        return user.username, user
    if frm.get("username"):
        return "@" + str(frm["username"]), None
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
    return name or uid or "?", None


# --- действия кнопок ------------------------------------------------------------------------

def _ref(row: TgAlert) -> tuple[str, list[int]]:
    kind, _, ids = (row.ref or "").partition(":")
    out = []
    for x in ids.split(","):
        try:
            out.append(int(x))
        except ValueError:
            pass
    return kind, out


def _parse(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def _mute(session, row: TgAlert, until: datetime | None) -> dict | None:
    """Глушит вид алерта row.kind у объекта; возвращает, как вернуть. None - объекта нет."""
    obj, ids = _ref(row)
    now = datetime.now(timezone.utc)
    if obj == "srv" and ids:
        s = await session.get(Server, ids[0])
        if s is None:
            return None
        if until is not None:
            prev = (s.alert_snoozes or {}).get(row.kind)
            snoozes = {k: v for k, v in (s.alert_snoozes or {}).items()
                       if k != row.kind and (_parse(v) or now) > now}
            snoozes[row.kind] = until.isoformat()
            s.alert_snoozes = snoozes
            return {"t": "snooze", "prev": prev}
        mutes = list(s.alert_mutes or [])
        added = row.kind not in mutes
        if added:
            s.alert_mutes = mutes + [row.kind]
        return {"t": "mute", "added": added}
    if obj == "chk" and ids:
        checks = [c for c in [await session.get(Check, i) for i in ids] if c is not None]
        if not checks:
            return None
        if until is not None:
            prev = {}
            for c in checks:
                prev[str(c.id)] = _aware(c.snooze_until).isoformat() if c.snooze_until else None
                c.snooze_until = until
            return {"t": "chk_snooze", "prev": prev}
        added = []
        for c in checks:
            mutes = list(c.alert_mutes or [])
            if row.kind not in mutes:
                c.alert_mutes = mutes + [row.kind]
                added.append(c.id)
        return {"t": "chk_mute", "added": added}
    return None


async def _unmute(session, row: TgAlert, how: dict) -> None:
    obj, ids = _ref(row)
    t = how.get("t")
    if obj == "srv" and ids:
        s = await session.get(Server, ids[0])
        if s is None:
            return
        if t == "snooze":
            snoozes = dict(s.alert_snoozes or {})
            snoozes.pop(row.kind, None)
            prev = _parse(how.get("prev"))
            if prev is not None and prev > datetime.now(timezone.utc):
                snoozes[row.kind] = prev.isoformat()
            s.alert_snoozes = snoozes
        elif t == "mute" and how.get("added"):
            s.alert_mutes = [m for m in (s.alert_mutes or []) if m != row.kind]
    elif obj == "chk":
        for i in ids:
            c = await session.get(Check, i)
            if c is None:
                continue
            if t == "chk_snooze":
                c.snooze_until = _parse((how.get("prev") or {}).get(str(i)))
            elif t == "chk_mute" and i in (how.get("added") or []):
                c.alert_mutes = [m for m in (c.alert_mutes or []) if m != row.kind]


async def apply(session, row: TgAlert, op: str, who: str, user: User | None,
                now: datetime, tz: str) -> tuple[str, str]:
    """Нажатие кнопки: (текст всплывашки, запись для журнала действий; пусто - не писать)."""
    if user is not None and user.role == "viewer" and op != tg_buttons.OP_ACK:
        return "Нужны права оператора", ""
    undo = dict(row.undo or {})
    if op == tg_buttons.OP_ACK:
        if row.acked_by:
            return f"Уже взял {row.acked_by}", ""
        row.acked_by, row.acked_at = who[:128], now
        return "Взял", "tg_ack"
    if op == tg_buttons.OP_US:
        obj, ids = _ref(row)
        if row.kind != "access_change" or obj != "srv" or not ids:
            return "Не к чему применить", ""
        if undo.get("us"):
            return "Уже отмечено", ""
        s = await session.get(Server, ids[0])
        if s is None:
            return "Сервер удален", ""
        cur = dict(s.changes or {}) if isinstance(s.changes, dict) else {}
        cur["ack_ts"], cur["ack_by"] = now.isoformat(), who
        s.changes = cur
        undo["us"], undo["us_by"] = True, who[:64]
        row.undo = undo
        return "Отмечено: это мы", "changes_ack"
    if op == tg_buttons.OP_UNDO:
        how = undo.pop("mute", None)
        if how is None:
            return "Уже не заглушено", ""
        await _unmute(session, row, how)
        row.undo = undo
        row.muted = f"↩️ {who} вернул алерты в {tg_buttons.hhmm(now, tz, now)}"
        return "Алерты вернулись", "tg_unmute"
    if "mute" in undo:
        return "Уже заглушено", ""
    if op == tg_buttons.OP_HOUR:
        until, label = now + timedelta(hours=1), "на 1 ч"
    elif op == tg_buttons.OP_MORNING:
        until, label = tg_buttons.next_morning(now, tz), "до утра"
    else:
        until, label = None, "насовсем"
    how = await _mute(session, row, until)
    if how is None:
        return "Объект удален из панели", ""
    undo["mute"] = how
    row.undo = undo
    tail = f", до {tg_buttons.hhmm(until, tz, now)}" if until is not None else ""
    row.muted = f"🔕 {who}: заглушено {label}{tail}"
    return f"Заглушено {label}", "tg_mute"


async def _refresh(cfg: dict, row: TgAlert, tz: str) -> None:
    """Дописать статус во все копии алерта и поменять кнопки."""
    text = tg_buttons.row_text(row, tz)
    markup = tg_buttons.row_keyboard(row)
    for m in row.messages or []:
        try:
            await _call(cfg, "editMessageText", {
                "chat_id": m.get("chat"), "message_id": m.get("id"), "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup,
            })
        except Exception:  # noqa: BLE001 - одна недоступная копия не мешает остальным
            log.warning("не поправлено сообщение %s", m, exc_info=True)


async def handle_callback(session_factory, cfg: dict, cq: dict, now: datetime) -> None:
    m = _CB.fullmatch(str(cq.get("data") or ""))
    toast = ""
    row = None
    if m:
        aid, op = int(m.group(1)), m.group(2)
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
        async with session_factory() as session:
            row = await session.get(TgAlert, aid)
            if row is None or chat not in {str(x.get("chat")) for x in (row.messages or [])}:
                toast, row = "Алерт устарел", None
            else:
                who, user = await _who(session, cq.get("from") or {})
                toast, action = await apply(session, row, op, who, user, now, cfg.get("tz") or "UTC")
                await session.commit()
                if action:
                    await audit.record(session, f"tg:{who}", action, row.target,
                                       f"{row.kind} {op}")
                    await session.refresh(row)
                else:
                    row = None
    try:
        await _call(cfg, "answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": toast})
    except Exception:  # noqa: BLE001
        pass
    if row is not None:
        await _refresh(cfg, row, cfg.get("tz") or "UTC")


# --- команды ----------------------------------------------------------------------------------

def _cut(lines: list[str]) -> str:
    out, size = [], 0
    for i, ln in enumerate(lines):
        if size + len(ln) > _MAX_TEXT:
            out.append(f"...и еще {len(lines) - i}")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out)


def _label(kind: str) -> str:
    return settings_store.SERVER_ALERT_KINDS.get(kind, (kind, ""))[0]


def _server_mutes(s: Server, now: datetime) -> set[str]:
    mutes = set(m for m in (s.alert_mutes or []) if ":" not in m)
    for k, v in (s.alert_snoozes or {}).items():
        t = _parse(v)
        if t is not None and t > now:
            mutes.add(k)
    return mutes


async def status_text(session, user: User | None, now: datetime) -> str:
    """Что сейчас горит: активные алерты серверов (alert_state) и лежащие сайты."""
    servers = (await session.scalars(select(Server).where(Server.enabled.is_(True)))).all()
    checks = (await session.scalars(select(Check).where(Check.enabled.is_(True)))).all()
    lines, n_srv = [], 0
    for s in sorted(servers, key=lambda x: x.name):
        if user is not None and not alerts.wants(user, alerts.Msg("", "servers", s.group_name or "")):
            continue
        st = s.alert_state or {}
        active = [k for k, v in st.items()
                  if k in settings_store.SERVER_ALERT_KINDS and isinstance(v, int) and not isinstance(v, bool) and v > 0]
        if not active:
            continue
        n_srv += 1
        mutes = _server_mutes(s, now)
        snoozed = s.snooze_until is not None and _aware(s.snooze_until) > now
        parts = []
        for k in sorted(active, key=lambda k: (k != "offline", -int(st.get(k) or 0), k)):
            parts.append(("🔕 " if (snoozed or k in mutes) else "") + _label(k))
        lines.append(f"<b>{html.escape(s.name)}</b>: {html.escape(', '.join(parts))}")
    down = [c for c in checks if c.last_status == "down"
            and (user is None or alerts.wants(user, alerts.Msg("", "sites", c.group_name or "")))]
    if down:
        lines.append("")
        lines.append(f"<b>Сайты лежат: {len(down)}</b>")
        for c in sorted(down, key=lambda x: x.name):
            snz = c.snooze_until is not None and _aware(c.snooze_until) > now
            lines.append(("🔕 " if snz else "• ") + html.escape(c.name))
    if not lines:
        return f"✅ Все в порядке: серверов {len(servers)}, сайтов {len(checks)}"
    head = f"🔴 Горит: серверов {n_srv}" + (f", сайтов {len(down)}" if down else "")
    return _cut([head, ""] + lines)


async def mutes_text(session, user: User | None, now: datetime, tz: str) -> str:
    lines = []
    if await settings_store.get_muted(session):
        lines.append("⏸ Все алерты на паузе")
    for s in sorted((await session.scalars(select(Server))).all(), key=lambda x: x.name):
        if user is not None and not alerts.wants(user, alerts.Msg("", "servers", s.group_name or "")):
            continue
        parts = []
        if s.snooze_until is not None and _aware(s.snooze_until) > now:
            parts.append(f"весь сервер до {tg_buttons.hhmm(_aware(s.snooze_until), tz, now)}")
        for k, v in sorted((s.alert_snoozes or {}).items()):
            t = _parse(v)
            if t is not None and t > now:
                parts.append(f"{_label(k)} до {tg_buttons.hhmm(t, tz, now)}")
        forever = sorted(m for m in (s.alert_mutes or []) if ":" not in m)
        if forever:
            parts.append("насовсем: " + ", ".join(_label(k) for k in forever))
        points = [m for m in (s.alert_mutes or []) if ":" in m]
        if points:
            parts.append(f"точечно: {len(points)}")
        if parts:
            lines.append(f"<b>{html.escape(s.name)}</b>: {html.escape('; '.join(parts))}")
    for c in sorted((await session.scalars(select(Check))).all(), key=lambda x: x.name):
        if user is not None and not alerts.wants(user, alerts.Msg("", "sites", c.group_name or "")):
            continue
        parts = []
        if c.snooze_until is not None and _aware(c.snooze_until) > now:
            parts.append(f"до {tg_buttons.hhmm(_aware(c.snooze_until), tz, now)}")
        if c.alert_mutes:
            parts.append("насовсем: " + ", ".join(sorted(c.alert_mutes)))
        if parts:
            lines.append(f"🌐 {html.escape(c.name)}: {html.escape('; '.join(parts))}")
    if not lines:
        return "🔔 Ничего не заглушено"
    return _cut(["🔕 Заглушено", ""] + lines)


async def _chat_user(session, cfg: dict, chat: str) -> tuple[bool, User | None]:
    """Можно ли отвечать в этот чат и чей он: общий чат алертов или личный чат учетки,
    которая получает алерты через общего бота."""
    if chat and chat == str(cfg.get("telegram_chat") or ""):
        return True, None
    user = await session.scalar(select(User).where(User.tg_chat_id == chat))
    if user is not None and (not user.tg_token or user.tg_token == cfg.get("telegram_token")):
        return True, user
    return False, None


async def _link(session, cfg: dict, msg: dict, code: str) -> str | None:
    """"/link CODE" из окна "Мои алерты в Telegram": пока панель читает бота сама, ответ ищет
    она, а не кнопка "Проверить привязку" (той апдейты уже не достанутся)."""
    user = await session.scalar(select(User).where(User.tg_link_code == code))
    if user is None or (user.tg_token and user.tg_token != cfg.get("telegram_token")):
        return None
    chat = str((msg.get("chat") or {}).get("id") or "")
    user.tg_chat_id, user.tg_link_code = chat, ""
    await session.commit()
    await audit.record(session, user.username, "tg_link", chat)
    return f"✅ Привязано к учетке {html.escape(user.username)}. Алерты по вашим серверам и сайтам будут приходить сюда."


async def handle_message(session_factory, cfg: dict, msg: dict, now: datetime) -> None:
    text = str(msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    cmd = parts[0].split("@", 1)[0].lower()
    chat = str((msg.get("chat") or {}).get("id") or "")
    tz = cfg.get("tz") or "UTC"
    reply = None
    async with session_factory() as session:
        if cmd == "/link" and len(parts) > 1:
            reply = await _link(session, cfg, msg, parts[1].strip().upper())
        elif cmd in ("/status", "/mutes", "/help", "/start"):
            ok, user = await _chat_user(session, cfg, chat)
            if not ok:
                if cmd == "/start":
                    reply = "Это бот панели мониторинга. Чтобы получать алерты, привяжите Telegram в панели: \"Мои алерты в Telegram\"."
            elif cmd == "/status":
                reply = await status_text(session, user, now)
            elif cmd == "/mutes":
                reply = await mutes_text(session, user, now, tz)
            else:
                reply = html.escape(HELP)
    if reply:
        try:
            await _call(cfg, "sendMessage", {"chat_id": chat, "text": reply, "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
        except Exception:  # noqa: BLE001
            log.warning("ответ боту не отправлен", exc_info=True)


async def handle_update(session_factory, cfg: dict, upd: dict, now: datetime) -> None:
    if upd.get("callback_query"):
        await handle_callback(session_factory, cfg, upd["callback_query"], now)
    elif upd.get("message"):
        await handle_message(session_factory, cfg, upd["message"], now)


# --- опрос ----------------------------------------------------------------------------------

async def _save_state(session_factory, st: dict, force: bool = False) -> None:
    async with session_factory() as session:
        old = await settings_store.get_tg_poll(session)
        changed = any(old.get(k) != st.get(k) for k in ("tok", "offset", "status", "error", "bot"))
        last = _parse(old.get("ts"))
        stale = last is None or (datetime.now(timezone.utc) - last).total_seconds() >= _STATE_EVERY_S
        if force or changed or stale:
            st = {**old, **st, "ts": datetime.now(timezone.utc).isoformat()}
            await settings_store.set_tg_poll(session, st)


async def poll_once(session_factory, settings: Settings) -> float:
    """Один заход опроса. Возвращает, сколько ждать до следующего."""
    async with session_factory() as session:
        cfg = await settings_store.get_alert_config(session, settings)
        st = await settings_store.get_tg_poll(session)
    if not alerts.buttons_on(cfg):
        if st.get("status") not in (None, "off"):
            await _save_state(session_factory, {"status": "off", "error": ""}, force=True)
        return _IDLE
    tok = _tok_hash(cfg["telegram_token"])
    offset = st.get("offset") if st.get("tok") == tok else None
    payload: dict = {"timeout": _POLL_TIMEOUT, "allowed_updates": ["message", "callback_query"]}
    if offset is None:
        # первый заход на этом боте: старое в очереди не разбираем (команды недельной давности
        # и чужие сообщения), только берем, с какого апдейта начинать
        payload = {"offset": -1, "timeout": 0, "allowed_updates": ["message", "callback_query"]}
    else:
        payload["offset"] = offset
    try:
        res = await _call(cfg, "getUpdates", payload, timeout=_POLL_TIMEOUT + 15)
    except Exception as exc:  # noqa: BLE001
        await _save_state(session_factory, {"tok": tok, "status": "error",
                                            "error": f"{type(exc).__name__}: {exc}"[:300]})
        return _BACKOFF_ERR
    if not res.get("ok"):
        code = res.get("error_code")
        desc = str(res.get("description") or "")[:300]
        status = "conflict" if code == 409 else "error"
        await _save_state(session_factory, {"tok": tok, "status": status, "error": desc})
        return _BACKOFF_CONFLICT if code in (401, 404, 409) else _BACKOFF_ERR
    updates = res.get("result") or []
    now = datetime.now(timezone.utc)
    if offset is None:
        nxt = (updates[-1]["update_id"] + 1) if updates else 0
    else:
        nxt = offset
        for upd in updates:
            nxt = max(nxt, int(upd.get("update_id", 0)) + 1)
            try:
                await handle_update(session_factory, cfg, upd, now)
            except Exception:  # noqa: BLE001 - один кривой апдейт не должен стопорить очередь
                log.exception("апдейт бота не разобран")
    bot = st.get("bot") if st.get("tok") == tok else None
    if not bot:
        try:
            me = await _call(cfg, "getMe", {})
            bot = "@" + str((me.get("result") or {}).get("username") or "")
        except Exception:  # noqa: BLE001
            bot = ""
    await _save_state(session_factory, {"tok": tok, "offset": nxt, "status": "ok", "error": "",
                                        "bot": bot})
    return 0


async def _cleanup(session_factory) -> None:
    try:
        async with session_factory() as session:
            cut = datetime.now(timezone.utc) - timedelta(days=_KEEP_DAYS)
            await session.execute(delete(TgAlert).where(TgAlert.ts < cut))
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("старые записи кнопок не вычищены", exc_info=True)


async def poll_loop(session_factory, settings: Settings) -> None:
    last_clean: datetime | None = None
    while True:
        try:
            wait = await poll_once(session_factory, settings)
        except Exception:  # noqa: BLE001
            log.exception("опрос бота упал")
            wait = _BACKOFF_ERR
        now = datetime.now(timezone.utc)
        if last_clean is None or now - last_clean > timedelta(days=1):
            await _cleanup(session_factory)
            last_clean = now
        if wait:
            await asyncio.sleep(wait)


def poll_state_out(st: dict) -> dict:
    """Для окна настроек: статус опроса без offset и хэша токена."""
    return {"status": st.get("status") or "off", "error": st.get("error") or "",
            "ts": st.get("ts") or "", "bot": st.get("bot") or ""}

