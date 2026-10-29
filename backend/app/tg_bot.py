"""Бот панели: нажатия кнопок под алертами и команды /status, /mutes, /copies.

Апдейты забираем long polling'ом (getUpdates), а не вебхуком: панели стоят за белым списком,
Telegram до них не достучится. Читать апдейты бота может только один потребитель: если этого
бота уже читает другая программа или вторая панель, Telegram отвечает 409 - тогда показываем
это в настройках и ждем, а не воюем за апдейты.

Кто вправе жать кнопки: "взял" - всякий в чате, куда ушел алерт. Глушить, "это мы" и "вернуть" -
только тот, чей Telegram привязан к учетке панели с правом правки (оператор, админ): в групповом
чате бывают гости, и любой из них навсегда глушил алерты, а "это мы" гасило алерт о смене доступа.
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

from app import alerts, audit, known, maint, settings_store, tg_buttons
from app import kube_expiry as kube_expiry_mod
from app.config import Settings, get_settings
from app.models import Check, MaintenanceWindow, Server, TgAlert, User

log = logging.getLogger("kervax.tg_bot")

_POLL_TIMEOUT = 25        # long polling: Telegram держит запрос до 25 с, если апдейтов нет
_BACKOFF_ERR = 30         # сеть или прокси к Telegram не отвечает
_BACKOFF_CONFLICT = 300   # бота читает кто-то еще
_IDLE = 20                # кнопки выключены: раз в 20 с смотрим, не включили ли
_KEEP_DAYS = 30           # записи кнопок старше месяца не нужны: такие алерты никто не жмет
_STATE_EVERY_S = 60       # "последний опрос" в настройках обновляем не чаще раза в минуту
_CB = re.compile(r"kx:(\d+):(" + "|".join(tg_buttons.OPS) + r")")
_CB_COPIES = re.compile(r"kxc:(" + "|".join(alerts.DUP_OPS) + r")")
_MAX_TEXT = 3900

HELP = (
    "Команды:\n"
    "/status - что сейчас горит\n"
    "/mutes - что заглушено\n"
    "/id - id этого чата для настроек панели\n"
    "/copies - не дублировать в основной чат то, что ушло в чаты групп (на 2 ч, до утра)\n\n"
    "Кнопки под алертом: заглушить то, о чем алерт (юнит, крон, под, контейнер), на час, до 9 утра "
    "или насовсем, \"взял\" - отметить, что занимаешься. Заглушенное возвращает кнопка \"Вернуть "
    "алерты\". Глушить могут только те, кто привязал Telegram к учетке панели (/link)."
)
# Код /link: живет 10 минут и принимается только в личном чате с ботом. Хранится как
# "<код>.<unix-время выдачи>"; старый код без времени не принимается
LINK_TTL_S = 600
LINK_LEN = 8


def link_code_store(code: str, now: datetime) -> str:
    return f"{code}.{int(now.timestamp())}"


def link_code_parse(stored: str, now: datetime) -> str:
    """Действующий код из записи учетки или пусто (просрочен, старый формат)."""
    code, _, ts = (stored or "").partition(".")
    if not code or not ts.isdigit() or now.timestamp() - int(ts) > LINK_TTL_S:
        return ""
    return code


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


def _item_keys(row: TgAlert) -> list[str]:
    """Поштучные ключи вещей, о которых алерт (tg_buttons.ITEM_MUTE); пусто - глушится вид."""
    keys = [str(k) for k in (row.undo or {}).get("keys") or [] if k]
    spec = tg_buttons.ITEM_MUTE.get(row.kind)
    return [k for k in keys if spec and k.startswith(spec[0])]


def _kexp_items(s: Server, keys: list[str]) -> dict[str, dict]:
    """Пункты сроков кластера из отчета по ключам "kube_expiry:<вид>|<где>"."""
    want = {k[len("kube_expiry:"):] for k in keys}
    return {kube_expiry_mod.key(it): it for it in (s.last_report or {}).get("kube_expiry") or []
            if isinstance(it, dict) and kube_expiry_mod.key(it) in want}


async def _mute(session, row: TgAlert, until: datetime | None, who: str = "") -> dict | None:
    """Глушит то, о чем алерт: вещи алерта поштучно, где это есть, иначе вид row.kind у объекта;
    возвращает, как вернуть. None - объекта нет."""
    obj, ids = _ref(row)
    now = datetime.now(timezone.utc)
    keys = _item_keys(row)
    spec = tg_buttons.ITEM_MUTE.get(row.kind)
    if obj == "srv" and ids:
        # несколько серверов - одно сообщение про один сбой на всех (alerts.merge_storm)
        servers = [s for s in [await session.get(Server, i) for i in ids] if s is not None]
        if not servers:
            return None
        if keys and until is not None and spec and spec[1]:
            # на время - поштучный снуз
            kprev: dict[str, dict] = {}
            for s in servers:
                snoozes = {k: v for k, v in (s.alert_snoozes or {}).items() if (_parse(v) or now) > now}
                kprev[str(s.id)] = {k: (s.alert_snoozes or {}).get(k) for k in keys}
                for k in keys:
                    snoozes[k] = until.isoformat()
                s.alert_snoozes = snoozes
            return {"t": "snooze_keys", "keys": keys, "prev": kprev}
        if keys and until is None and row.kind == "kube_expiry":
            # сроки кластера: отметка "не следить" у пункта, снимется сама, когда секрет обновят
            done: dict[str, list[str]] = {}
            for s in servers:
                cur = dict(s.kube_expiry_ignored or {}) if isinstance(s.kube_expiry_ignored, dict) else {}
                for ik, it in _kexp_items(s, keys).items():
                    if ik not in cur:
                        cur[ik] = kube_expiry_mod.entry(it, who or "telegram", now)
                        done.setdefault(str(s.id), []).append(ik)
                s.kube_expiry_ignored = cur or None
            if done:
                return {"t": "kexp", "added": done}
        elif keys and until is None:
            added_k: dict[str, list[str]] = {}
            for s in servers:
                mutes = list(s.alert_mutes or [])
                new = [k for k in keys if k not in mutes]
                if new:
                    s.alert_mutes = mutes + new
                    added_k[str(s.id)] = new
            return {"t": "mute_keys", "added": added_k}
        if until is not None:
            prev = {}
            for s in servers:
                prev[str(s.id)] = (s.alert_snoozes or {}).get(row.kind)
                snoozes = {k: v for k, v in (s.alert_snoozes or {}).items()
                           if k != row.kind and (_parse(v) or now) > now}
                snoozes[row.kind] = until.isoformat()
                s.alert_snoozes = snoozes
            return {"t": "snooze", "prev": prev}
        added = []
        for s in servers:
            mutes = list(s.alert_mutes or [])
            if row.kind not in mutes:
                s.alert_mutes = mutes + [row.kind]
                added.append(s.id)
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
        for n, i in enumerate(ids):
            s = await session.get(Server, i)
            if s is None:
                continue
            if t == "snooze_keys":
                p = (how.get("prev") or {}).get(str(i)) or {}
                snoozes = dict(s.alert_snoozes or {})
                for k in how.get("keys") or []:
                    snoozes.pop(k, None)
                    prev = _parse(p.get(k))
                    if prev is not None and prev > datetime.now(timezone.utc):
                        snoozes[k] = prev.isoformat()
                s.alert_snoozes = snoozes
            elif t == "mute_keys":
                gone = set((how.get("added") or {}).get(str(i)) or [])
                if gone:
                    s.alert_mutes = [m for m in (s.alert_mutes or []) if m not in gone]
            elif t == "kexp":
                gone = set((how.get("added") or {}).get(str(i)) or [])
                if gone and isinstance(s.kube_expiry_ignored, dict):
                    s.kube_expiry_ignored = {k: v for k, v in s.kube_expiry_ignored.items() if k not in gone} or None
            elif t == "snooze":
                # до 1.4.246 prev и added - про один сервер, без словаря и списка
                p = how.get("prev")
                snoozes = dict(s.alert_snoozes or {})
                snoozes.pop(row.kind, None)
                prev = _parse(p.get(str(i)) if isinstance(p, dict) else (p if n == 0 else None))
                if prev is not None and prev > datetime.now(timezone.utc):
                    snoozes[row.kind] = prev.isoformat()
                s.alert_snoozes = snoozes
            elif t == "mute":
                a = how.get("added")
                if (i in a) if isinstance(a, list) else (bool(a) and n == 0):
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
    if op != tg_buttons.OP_ACK and user is None:
        return "Привяжите Telegram к учетке панели (/link), тогда кнопки заработают", ""
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
    how = await _mute(session, row, until, who)
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
        await alerts.tg_edit(cfg, m.get("chat"), m.get("id"), text, markup)


# --- эскалация --------------------------------------------------------------------------------

async def _srv_wanted(session, row: TgAlert, sid: int, now: datetime) -> bool | None:
    s = await session.get(Server, sid)
    if s is None or not s.enabled:
        return False
    if (s.snooze_until is not None and _aware(s.snooze_until) > now) or row.kind in _server_mutes(s, now):
        return False
    keys = _item_keys(row)
    if keys:
        # глушили поштучно (кнопкой или в панели): все вещи алерта заглушены - не повторять
        cur = set(s.alert_mutes or []) | {k for k, v in (s.alert_snoozes or {}).items()
                                          if (_parse(v) or now) > now}
        ign = s.kube_expiry_ignored if isinstance(s.kube_expiry_ignored, dict) else {}
        if all(k in cur or k[len("kube_expiry:"):] in ign for k in keys):
            return False
    lvl = (s.alert_state or {}).get(row.kind)
    if isinstance(lvl, int) and not isinstance(lvl, bool) and lvl <= 0:
        return None
    return True


async def _still_wanted(session, row: TgAlert, now: datetime) -> bool | None:
    """Стоит ли еще повторять: True - да, False - заглушили в панели или объекта нет (больше не
    повторять), None - беда ушла, а отбой не пришел (заглушен, мигание) - отметить решенным."""
    obj, ids = _ref(row)
    if obj == "srv" and ids:
        # про несколько серверов: повторять, пока не вернутся все незаглушенные
        res = [await _srv_wanted(session, row, i, now) for i in ids]
        if True in res:
            return True
        return None if None in res else False
    if obj == "chk" and ids:
        c = await session.get(Check, ids[0])
        if c is None or not c.enabled:
            return False
        if (c.snooze_until is not None and _aware(c.snooze_until) > now) or row.kind in (c.alert_mutes or []):
            return False
        if row.kind == "down" and c.last_status != "down":
            return None
        return True
    return False


async def escalate(session_factory, settings: Settings, now: datetime | None = None) -> int:
    """Повтор алертов, на которые никто не нажал "взял": через N минут - в чат эскалации и
    выбранным учеткам, и так до заданного числа повторов. Возвращает, сколько повторено."""
    now = now or datetime.now(timezone.utc)
    async with session_factory() as session:
        cfg = await settings_store.get_alert_config(session, settings)
        esc = await settings_store.get_escalation(session)
        if not (alerts.buttons_on(cfg) and esc["enabled"]) or await settings_store.get_muted(session):
            return 0
        # бот не читается (409, сеть): "взял" до нас не доходит, и повтор был бы враньем
        if (await settings_store.get_tg_poll(session)).get("status") != "ok":
            return 0
        chats = [esc["chat"]] if esc["chat"] else []
        if esc["users"]:
            for u in (await session.scalars(select(User).where(User.username.in_(esc["users"])))).all():
                if u.tg_chat_id and (not u.tg_token or u.tg_token == cfg.get("telegram_token")):
                    chats.append(u.tg_chat_id)
        chats = list(dict.fromkeys(c for c in chats if c))
        if not chats:
            return 0
        after = timedelta(minutes=esc["after_min"])
        rows = (await session.scalars(select(TgAlert).where(
            TgAlert.acked_by == "", TgAlert.resolved_at.is_(None),
            TgAlert.esc_level < esc["repeat"], TgAlert.kind.in_(esc["kinds"]),
            TgAlert.ts > now - timedelta(days=2)))).all()
        tz = cfg.get("tz") or "UTC"
        done = 0
        for row in rows:
            start = _aware(row.esc_at) or _aware(row.ts) or now
            if now - start < after or "mute" in (row.undo or {}):
                continue
            wanted = await _still_wanted(session, row, now)
            if wanted is None:
                row.resolved_at = now
                continue
            if not wanted:
                row.esc_level = esc["repeat"]  # заглушили руками: больше не повторяем
                continue
            mins = int((now - _aware(row.ts)).total_seconds() // 60) if row.ts else esc["after_min"]
            text = f"⏰ Никто не взял за {mins} мин\n\n" + tg_buttons.row_text(row, tz, now)
            sent = []
            for chat in chats:
                try:
                    mid = await alerts.tg_send(cfg.get("telegram_api", ""), cfg["telegram_token"], chat,
                                               text, "HTML", tg_buttons.row_keyboard(row))
                    if mid is not None:
                        sent.append({"chat": str(chat), "id": mid})
                except Exception as exc:  # noqa: BLE001
                    log.warning("эскалация в %s не отправлена: %s", chat, exc)
            if not sent:
                continue  # никуда не ушло - попробуем на следующем заходе
            row.messages = list(row.messages or []) + sent
            row.esc_level, row.esc_at = row.esc_level + 1, now
            done += 1
        await session.commit()
    return done


async def dup_catchup(session_factory, settings: Settings, now: datetime | None = None) -> int:
    """Критичное, которое пауза копий придержала (alerts.dup_held): в группе за DUP_HOLD_MIN
    никто не нажал "взял", а беда не прошла - отдать в основной чат. Возвращает, сколько отдано."""
    now = now or datetime.now(timezone.utc)
    add: list[list[str]] = []
    done = 0
    async with session_factory() as session:
        cfg = await settings_store.get_alert_config(session, settings)
        if not cfg.get("telegram_token") or await settings_store.get_muted(session):
            return 0
        rows = [r for r in (await session.scalars(select(TgAlert).where(
            TgAlert.resolved_at.is_(None), TgAlert.ts > now - timedelta(days=1)))).all()
            if (r.undo or {}).get("held")]
        tz = cfg.get("tz") or "UTC"
        after = timedelta(minutes=alerts.DUP_HOLD_MIN)
        for row in rows:
            undo = dict(row.undo or {})
            chat = str(undo.pop("held"))
            if row.acked_by or "mute" in undo:
                row.undo = undo  # взяли или заглушили в группе - основному чату не нужно
                continue
            if now - (_aware(row.ts) or now) < after:
                continue
            wanted = await _still_wanted(session, row, now)
            if not wanted:
                if wanted is None:
                    row.resolved_at = now
                row.undo = undo
                continue
            mins = int((now - _aware(row.ts)).total_seconds() // 60)
            text = f"⏰ В чате группы никто не взял за {mins} мин\n\n" + tg_buttons.row_text(row, tz, now)
            try:
                mid = await alerts.tg_send(cfg.get("telegram_api", ""), cfg["telegram_token"], chat,
                                           text, "HTML", tg_buttons.row_keyboard(row))
            except Exception as exc:  # noqa: BLE001
                log.warning("придержанный алерт в %s не отправлен: %s", chat, exc)
                continue  # попробуем на следующем заходе
            if mid is not None:
                row.messages = list(row.messages or []) + [{"chat": chat, "id": mid}]
            row.undo = undo
            # у шторма в target имена через запятую, а отбой приходит по каждому серверу
            add += [[row.kind, t.strip()] for t in (row.target or "").split(", ") if t.strip()]
            done += 1
        await session.commit()
    if add:
        await alerts.note_passed(session_factory, add)
    return done


async def handle_callback(session_factory, cfg: dict, cq: dict, now: datetime) -> None:
    if _CB_COPIES.fullmatch(str(cq.get("data") or "")):
        await handle_copies(session_factory, cfg, cq, now)
        return
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
    # идущие окна обслуживания: строкой сверху, а их серверы и мониторы помечены 🚧
    tz = (await settings_store.get_alert_config(session, get_settings())).get("tz") or "Europe/Moscow"
    windows = list(await session.scalars(select(MaintenanceWindow)))
    m_srv, m_chk = maint.active(windows, now, tz)
    maint_lines = []
    for w in windows:
        cur = maint.current(w, now, tz)
        if cur is not None:
            maint_lines.append(f"🚧 Обслуживание: {html.escape(w.title)} до {tg_buttons.hhmm(cur[1], tz, now)}")
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
        # известные проблемы (app/known.py): про них знают, помечаем, а не прячем
        kn = {k for k, e in (s.known_issues or {}).items()
              if isinstance(e, dict) and not e.get("worse") and not known.expired(e, now)}
        parts = []
        for k in sorted(active, key=lambda k: (k != "offline", -int(st.get(k) or 0), k)):
            mark = "🔕 " if (snoozed or k in mutes) else ("📌 " if k in kn else "")
            parts.append(mark + _label(k))
        lines.append(("🚧 " if s.id in m_srv else "") + f"<b>{html.escape(s.name)}</b>: {html.escape(', '.join(parts))}")
    down = [c for c in checks if c.last_status == "down"
            and (user is None or alerts.wants(user, alerts.Msg("", "sites", c.group_name or "")))]
    if down:
        lines.append("")
        lines.append(f"<b>Сайты лежат: {len(down)}</b>")
        for c in sorted(down, key=lambda x: x.name):
            snz = c.snooze_until is not None and _aware(c.snooze_until) > now
            kn = isinstance(c.known_issue, dict) and not known.expired(c.known_issue, now)
            lines.append(("🚧 " if c.id in m_chk else "🔕 " if snz else "📌 " if kn else "• ") + html.escape(c.name))
    if not lines:
        ok = f"✅ Все в порядке: серверов {len(servers)}, сайтов {len(checks)}"
        return _cut([ok] + ([""] + maint_lines if maint_lines else []))
    head = f"🔴 Горит: серверов {n_srv}" + (f", сайтов {len(down)}" if down else "")
    return _cut([head] + maint_lines + [""] + lines)


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
    # чат по группам: /status и /mutes - только про его группы, как у учетки с этими группами
    route = next((r for r in (cfg.get("routes") or {}).get("chats") or [] if r["chat"] == chat), None)
    if route is not None:
        return True, User(username=route.get("name") or chat, sections=None,
                          server_groups=route["server_groups"] or ["\x00"],
                          site_groups=route["site_groups"] or ["\x00"])
    user = await session.scalar(select(User).where(User.tg_chat_id == chat))
    if user is not None and (not user.tg_token or user.tg_token == cfg.get("telegram_token")):
        return True, user
    return False, None


async def _link(session, cfg: dict, msg: dict, code: str, now: datetime | None = None) -> str | None:
    """"/link CODE" из окна "Мои алерты в Telegram": пока панель читает бота сама, ответ ищет
    она, а не кнопка "Проверить привязку" (той апдейты уже не достанутся). Код живет LINK_TTL_S
    и только в личном чате: в групповом его видят все, и привязать чужой чат к учетке - значит
    отдать ему кнопки под алертами."""
    now = now or datetime.now(timezone.utc)
    if not code or "." in code:
        return None
    if ((msg.get("chat") or {}).get("type") or "private") != "private":
        return "Код привязки принимается только в личном чате с ботом"
    user = await session.scalar(select(User).where(User.tg_link_code.like(f"{code}.%")))
    if user is None or link_code_parse(user.tg_link_code, now) != code:
        return None
    if user.tg_token and user.tg_token != cfg.get("telegram_token"):
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
    reply, markup = None, None
    async with session_factory() as session:
        if cmd == "/link" and len(parts) > 1:
            reply = await _link(session, cfg, msg, parts[1].strip().upper(), now)
        elif cmd == "/copies":
            ok, _, why = await _copies_allowed(session, cfg, chat, str((msg.get("chat") or {}).get("type")
                                                                       or "private"), msg.get("from") or {})
            if ok:
                reply, markup = copies_text(cfg, now), copies_keyboard(cfg, now)
            elif (await _chat_user(session, cfg, chat))[0]:
                reply = html.escape(why)
        elif cmd == "/id":
            # id чата для настроек панели (основной чат, чаты по группам, эскалация)
            reply = f"id этого чата: <code>{html.escape(chat)}</code>"
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
                                             "disable_web_page_preview": True,
                                             **({"reply_markup": markup} if markup else {})})
        except Exception:  # noqa: BLE001
            log.warning("ответ боту не отправлен", exc_info=True)


# --- пауза копий в основной чат -------------------------------------------------------------

def copies_text(cfg: dict, now: datetime) -> str:
    when = alerts.dup_pause_text(cfg, now)
    if when:
        return (f"⏸ Копии алертов групп сюда не идут {html.escape(when)}. Критичное (сервер недоступен, "
                "🚨) придет сюда, если в чате группы его не возьмут.")
    return "🔔 Сюда приходят копии алертов групп, у которых есть свой чат."


def copies_keyboard(cfg: dict, now: datetime) -> dict:
    if alerts.dup_paused(cfg, now):
        return {"inline_keyboard": [[{"text": "▶️ Вернуть копии", "callback_data": "kxc:off"}]]}
    return {"inline_keyboard": [[
        {"text": "⏸ На 2 ч", "callback_data": "kxc:2h"},
        {"text": "До утра", "callback_data": "kxc:morning"},
        {"text": "Пока не включу", "callback_data": "kxc:forever"},
    ]]}


async def _copies_allowed(session, cfg: dict, chat: str, chat_type: str, frm: dict) -> tuple[bool, str, str]:
    """Пауза копий - только из основного чата. Личный чат - его владельца, в групповом нужен
    привязанный Telegram с правом правки, как у кнопок "заглушить"."""
    if not chat or chat != str(cfg.get("telegram_chat") or ""):
        return False, "", "Копии ставятся на паузу из основного чата алертов"
    who, user = await _who(session, frm)
    if chat_type != "private" and (user is None or user.role == "viewer"):
        return False, who, "Привяжите Telegram к учетке панели с правом правки (/link)"
    return True, who, ""


async def handle_copies(session_factory, cfg: dict, cq: dict, now: datetime) -> None:
    op = _CB_COPIES.fullmatch(str(cq.get("data") or "")).group(1)
    chat_obj = (cq.get("message") or {}).get("chat") or {}
    chat = str(chat_obj.get("id") or "")
    async with session_factory() as session:
        ok, who, toast = await _copies_allowed(session, cfg, chat, str(chat_obj.get("type") or "private"),
                                               cq.get("from") or {})
        if ok:
            await settings_store.set_dup_pause(
                session, alerts.dup_pause_value(op, now, cfg.get("tz") or "UTC", who))
            await audit.record(session, f"tg:{who}", "alerts_dup_pause", "", op)
            cfg = {**cfg, "dup_pause": await settings_store.get_dup_pause(session)}
            toast = "Копии вернулись" if op == "off" else "Копии на паузе"
    try:
        await _call(cfg, "answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": toast})
    except Exception:  # noqa: BLE001
        pass
    mid = (cq.get("message") or {}).get("message_id")
    if ok and mid is not None:
        try:
            await _call(cfg, "editMessageText", {"chat_id": chat, "message_id": mid, "parse_mode": "HTML",
                                                 "text": copies_text(cfg, now),
                                                 "reply_markup": copies_keyboard(cfg, now)})
        except Exception:  # noqa: BLE001
            log.warning("сообщение о паузе копий не обновлено", exc_info=True)


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
        try:
            await escalate(session_factory, settings)
        except Exception:  # noqa: BLE001
            log.exception("эскалация упала")
        try:
            await dup_catchup(session_factory, settings)
        except Exception:  # noqa: BLE001
            log.exception("придержанные критичные не разобраны")
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

