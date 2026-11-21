"""Durably own bot reminders while retaining the original callback identity."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app import alert_delivery, notification_store, settings_store
from app.models import NotificationEvent, TgAlert


class _ObsoleteAction(Exception):
    pass


async def wanted(session, action: dict, now: datetime) -> bool:
    """Recheck the original row immediately before an action POST."""
    from app import tg_bot

    if action.get("kind") not in ("escalation", "held"):
        return False
    row = await session.get(TgAlert, action.get("row_id"), populate_existing=True)
    if row is None or not row.messages or row.acked_by or row.resolved_at or "mute" in (row.undo or {}):
        return False
    if action["kind"] == "escalation" and row.esc_level != action.get("level"):
        return False
    return await tg_bot._still_wanted(session, row, now) is True


async def enqueue(factory, cfg: dict, row_id: int, kind: str, level: int,
                  chats: list[str], message, now: datetime) -> bool:
    """Save action ownership and its observer patch in one transaction, without POST."""
    if kind not in ("escalation", "held"):
        raise ValueError("unknown bot delivery action")
    token = cfg.get("telegram_token") or ""
    if not token:
        return False
    recipients = []
    for chat in dict.fromkeys(str(chat) for chat in chats if chat):
        key, transport = alert_delivery._telegram_identity(cfg.get("telegram_api"), token, chat)
        recipients.append({"key": key, "transport": transport,
                           "target": {"type": "telegram", "bot": "common", "chat": chat,
                                      "route": "action", "required": True}})
    if not recipients:
        return False
    action = {"row_id": row_id, "kind": kind, "level": level}
    utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    spec = {"stream": f"tg-action:{row_id}:{kind}", "phase": "event", "members": [], "level": level,
            "observed_at": utc, "occurrence": f"{kind}:{level}" if kind == "escalation" else "held",
            "payload": {"message": alert_delivery.pack(message), "parse_mode": "HTML", "threshold": 0,
                        "action": action, "skip_history": True}, "recipients": recipients}

    async def admitted(session, _index, event_id):
        from app import tg_bot

        row = await session.scalar(select(TgAlert).where(TgAlert.id == row_id).with_for_update())
        if row is None or not row.messages or row.acked_by or row.resolved_at or "mute" in (row.undo or {}):
            raise _ObsoleteAction
        if await tg_bot._still_wanted(session, row, utc) is not True:
            raise _ObsoleteAction
        if kind == "escalation":
            if row.esc_level > level or level > row.esc_level + 1:
                raise _ObsoleteAction
            if row.esc_level < level:
                row.esc_level, row.esc_at = level, utc
        else:
            undo = dict(row.undo or {})
            if str(undo.get("held") or "") not in {item["target"]["chat"] for item in recipients}:
                raise _ObsoleteAction
            undo.pop("held", None)
            row.undo = undo or None
            def passed(current):
                for target in (row.target or "").split(", "):
                    pair = [row.kind, target.strip()]
                    if pair[1] and pair not in current:
                        current.append(pair)
                return current

            await settings_store.update_main_passed(session, passed)
        event = await session.get(NotificationEvent, event_id)
        event.tg_alert_id = row_id

    try:
        events = await notification_store.admit(factory, [spec], utc, callback=admitted)
    except _ObsoleteAction:
        return False
    return bool(events and events[0] is not None)
