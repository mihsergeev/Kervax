"""Кнопки под алертами в Telegram: клавиатура и запись об алерте, на которую она ссылается.

Здесь только то, что нужно при отправке (alerts.dispatch): какие сообщения получают кнопки,
как выглядит клавиатура, текст со строками "взял" и "заглушено". Нажатия разбирает tg_bot.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import TgAlert

log = logging.getLogger("kervax.tg_buttons")

# Действия кнопок (callback_data "kx:<id>:<op>", лимит Telegram 64 байта)
OP_HOUR, OP_MORNING, OP_FOREVER, OP_ACK, OP_US, OP_UNDO = "h1", "mo", "pm", "ak", "us", "un"
OPS = (OP_HOUR, OP_MORNING, OP_FOREVER, OP_ACK, OP_US, OP_UNDO)
MORNING_HOUR = 9


def zone(tz: str):
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001 - кривой пояс в настройках не должен ронять отправку
        return timezone.utc


def next_morning(now: datetime, tz: str) -> datetime:
    """Ближайшие 9:00 по поясу панели (уже после 9 - завтрашние)."""
    local = now.astimezone(zone(tz))
    at = local.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    return at.astimezone(timezone.utc)


def hhmm(t: datetime, tz: str, now: datetime | None = None) -> str:
    """Время для строки статуса: "15:20", а если не сегодня - "15:20 15.10"."""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)  # из SQLite время приходит без пояса, это UTC
    local = t.astimezone(zone(tz))
    ref = (now or datetime.now(timezone.utc)).astimezone(zone(tz))
    return local.strftime("%H:%M") if local.date() == ref.date() else local.strftime("%H:%M %d.%m")


def wants_buttons(m) -> bool:
    """Кнопки - у срабатываний, про которые известно, что глушить: сервер или монитор.
    Отбою кнопки ни к чему, дайджесту тоже (там десяток разных алертов)."""
    return bool(getattr(m, "ref", "")) and not getattr(m, "recovery", False)


def _btn(text: str, aid: int, op: str) -> dict:
    return {"text": text, "callback_data": f"kx:{aid}:{op}"}


def keyboard(aid: int, kind: str, *, muted: bool = False, acked: bool = False,
             us_done: bool = False) -> dict:
    rows = []
    if muted:
        rows.append([_btn("↩️ Вернуть алерты", aid, OP_UNDO)])
    else:
        rows.append([_btn("🔕 1 ч", aid, OP_HOUR), _btn("🔕 до утра", aid, OP_MORNING),
                     _btn("🔕 насовсем", aid, OP_FOREVER)])
    second = []
    if not acked:
        second.append(_btn("✋ Взял", aid, OP_ACK))
    if kind == "access_change" and not us_done:
        second.append(_btn("✅ Это мы", aid, OP_US))
    if second:
        rows.append(second)
    return {"inline_keyboard": rows}


def row_keyboard(row: TgAlert) -> dict:
    undo = row.undo or {}
    if row.resolved_at is not None:
        # беда ушла: глушить и брать нечего, но заглушенное кнопкой надо уметь вернуть
        return {"inline_keyboard": [[_btn("↩️ Вернуть алерты", row.id, OP_UNDO)]] if "mute" in undo else []}
    return keyboard(row.id, row.kind, muted="mute" in undo, acked=bool(row.acked_by),
                    us_done=bool(undo.get("us")))


def row_text(row: TgAlert, tz: str, now: datetime | None = None) -> str:
    """Исходный текст алерта и под ним кто что сделал. Telegram режет сообщение на 4096."""
    lines = []
    if row.acked_by and row.acked_at is not None:
        lines.append(f"✋ взял {_esc(row.acked_by)} в {hhmm(row.acked_at, tz, now)}")
    undo = row.undo or {}
    if undo.get("us"):
        lines.append(f"✅ {_esc(str(undo.get('us_by') or ''))}: это мы")
    if row.muted:
        lines.append(_esc(row.muted))
    if row.resolved_at is not None:
        lines.append(f"✅ снова в норме в {hhmm(row.resolved_at, tz, now)}")
    text = row.text or ""
    if lines:
        text = text[: 4000 - sum(len(x) + 1 for x in lines)] + "\n\n" + "\n".join(lines)
    return text


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def prepare(session_factory, messages: list) -> dict[int, int]:
    """Записи для сообщений с кнопками: {номер сообщения в пачке: id записи}. Сбой - без
    кнопок, но алерт уходит: кнопки удобство, а не условие доставки."""
    idx = [i for i, m in enumerate(messages) if wants_buttons(m)]
    if not idx:
        return {}
    try:
        async with session_factory() as session:
            rows = []
            for i in idx:
                m = messages[i]
                row = TgAlert(
                    ref=str(getattr(m, "ref", ""))[:255],
                    kind=str(getattr(m, "kind", "") or "")[:40],
                    target=str(getattr(m, "target", "") or "")[:255],
                    section=str(getattr(m, "section", "") or "")[:20],
                    grp=str(getattr(m, "group", "") or "")[:255],
                    text=str(m),
                    messages=[],
                )
                session.add(row)
                rows.append((i, row))
            await session.commit()
            return {i: row.id for i, row in rows}
    except Exception:  # noqa: BLE001
        log.warning("записи кнопок не созданы, алерты уйдут без кнопок", exc_info=True)
        return {}


async def store_sent(session_factory, sent: dict[int, list]) -> None:
    """Куда ушли копии: {id записи: [{"chat", "id"}]} - чтобы потом поправить их все."""
    sent = {k: v for k, v in sent.items() if v}
    if not sent:
        return
    try:
        async with session_factory() as session:
            for aid, msgs in sent.items():
                row = await session.get(TgAlert, aid)
                if row is not None:
                    row.messages = list(row.messages or []) + msgs
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("не записано, куда ушли алерты с кнопками", exc_info=True)
