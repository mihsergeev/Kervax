"""Кнопки под алертами в Telegram: клавиатура и запись об алерте, на которую она ссылается.

Здесь только то, что нужно при отправке (alerts.dispatch): какие сообщения получают кнопки,
как выглядит клавиатура, текст со строками "взял" и "заглушено". Нажатия разбирает tg_bot.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import TgAlert

log = logging.getLogger("kervax.tg_buttons")

# Действия кнопок (callback_data "kx:<id>:<op>", лимит Telegram 64 байта)
OP_HOUR, OP_MORNING, OP_FOREVER, OP_ACK, OP_US, OP_UNDO = "h1", "mo", "pm", "ak", "us", "un"
OPS = (OP_HOUR, OP_MORNING, OP_FOREVER, OP_ACK, OP_US, OP_UNDO)
MORNING_HOUR = 9

# Что глушит кнопка под алертом. Раньше - вид целиком: "заглушить" под алертом про один CronJob
# выключало алерты всех подов и кронов кластера, под алертом про nginx.service - всех юнитов
# сервера, и главная этого не видела. Для видов, где у алерта есть конкретные вещи, кнопка пишет
# их поштучные ключи (<префикс><ключ вещи>), те же, что ставит панель. Второе - понимает ли вид
# поштучный снуз на время; если нет, "1 ч" и "до утра" глушат вид целиком (ненадолго), а
# "насовсем" - все равно только эти вещи.
ITEM_MUTE: dict[str, tuple[str, bool]] = {
    "units": ("unit:", True), "workers": ("worker:", True), "queue_stuck": ("qstuck:", True),
    "queue_failed": ("qfail:", True), "cron_stuck": ("cron:", True), "log_errors": ("logerr:", True),
    "fpm": ("fpm:", True), "vpn_torrent": ("torrent:", True), "vpn_heavy": ("vpnheavy:", True), "gitlab": ("gitlab:", True), "autoban": ("autoban:", True),
    "kube_cron": ("kube_cron:", True), "kube_job": ("kube_job:", True), "kube_pod": ("kube_pod:", True),
    "docker_down": ("docker_down:", True), "docker_loop": ("docker_loop:", True),
    "kube_restarts": ("restarts:", False), "docker_restarts": ("restarts:", False),
    "mem_limit": ("memlim:", False), "mem_leak": ("leak:", False), "listen_gone": ("listen:", False),
    "cf_realip": ("cfip:", False), "docker_risk": ("dockrisk:", False),
    # сроки кластера "насовсем" - отметка "не следить" у пункта (app/kube_expiry.py): снимется сама,
    # когда секрет обновят
    "kube_expiry": ("kube_expiry:", False),
}


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


def _cut_html(text: str, limit: int) -> str:
    """Обрезать HTML алерта, не разрезав тег или сущность: Telegram на оборванный "<a hr" отвечает
    400, и правка сообщения ("взял", "заглушено") и эскалация не уходили."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # по последнему переводу строки: строки алерта собраны целыми тегами
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    # недописанный тег или сущность в хвосте
    lt, amp = cut.rfind("<"), cut.rfind("&")
    if lt > cut.rfind(">"):
        cut = cut[:lt]
    if amp > cut.rfind(";"):
        cut = cut[:amp]
    # открытые и не закрытые теги закрываем: <a>, <b>, <i>, <code>, <pre>
    opened: list[str] = []
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", cut):
        if m.group(1):
            if opened and opened[-1] == m.group(2):
                opened.pop()
        else:
            opened.append(m.group(2))
    return cut + "..." + "".join(f"</{t}>" for t in reversed(opened))


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
        text = _cut_html(text, 4000 - sum(len(x) + 1 for x in lines) - 20) + "\n\n" + "\n".join(lines)
    return _cut_html(text, 4000)


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
                # что именно глушить (ITEM_MUTE): ключи вещей из алерта
                keys = [str(k) for k in getattr(m, "mutes", None) or [] if k][:50]
                row = TgAlert(
                    ref=str(getattr(m, "ref", ""))[:255],
                    kind=str(getattr(m, "kind", "") or "")[:40],
                    target=str(getattr(m, "target", "") or "")[:255],
                    section=str(getattr(m, "section", "") or "")[:20],
                    grp=str(getattr(m, "group", "") or "")[:255],
                    text=str(m),
                    messages=[],
                    undo={"keys": keys} if keys else None,
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


async def hold(session_factory, ids: list[int], chat: str) -> None:
    """Критичное в паузу копий: запись помнит, в какой чат отдать его, если в группе не возьмут
    (tg_bot.dup_catchup)."""
    try:
        async with session_factory() as session:
            for aid in ids:
                row = await session.get(TgAlert, aid)
                if row is not None and row.messages:
                    row.undo = {**(row.undo or {}), "held": chat}
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("придержанные критичные не записаны", exc_info=True)
