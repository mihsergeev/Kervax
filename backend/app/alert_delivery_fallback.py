"""Queue a rejected group notification for the current main chat, without I/O."""
from datetime import datetime

from sqlalchemy import select

from app import notification_store as store
from app.models import NotificationDelivery as Delivery, NotificationTransport as Transport


async def enqueue(factory, cfg: dict, rows: list[dict], now: datetime) -> list[int]:
    """Return stable main-chat delivery ids for actually rejected group jobs.

    The caller supplies only permanently rejected outcomes it still owned.
    Persisted status and route are checked again here: uncertainty and personal
    subscriptions never become a group fallback. Credentials remain in settings.
    """
    token, chat = cfg.get("telegram_token") or "", str(cfg.get("telegram_chat") or "")
    ids = sorted({int(row["id"]) for row in rows})
    if not token or not chat or not ids:
        return []
    from app.alert_delivery import _telegram_identity

    recipient, transport = _telegram_identity(cfg.get("telegram_api"), token, chat)
    recipient, transport = store._identity(recipient), store._identity(transport)
    now = store._aware(now)
    async with factory() as session, session.begin():
        # Write first to serialize SQLite writers; the delivery unique constraint
        # also arbitrates concurrent enqueue calls on PostgreSQL.
        await store._insert_once(session, Transport, dict(key=transport, failures=0, lease_owner=""), ["key"])
        source = list(await session.scalars(select(Delivery).where(
            Delivery.id.in_(ids), Delivery.status == "rejected").order_by(Delivery.id)))
        events = sorted({row.event_id for row in source if (row.target or {}).get("type") == "telegram"
                         and (row.target or {}).get("route") == "group"
                         and (row.target or {}).get("bot") == "common"
                         and (row.target or {}).get("required")})
        for event_id in events:
            await store._insert_once(session, Delivery, dict(
                event_id=event_id, recipient_key=recipient, transport_key=transport,
                target={"type": "telegram", "route": "fallback", "bot": "common", "chat": chat,
                        "required": True, "reason": "group_rejected"},
                status="pending", attempts=0, uncertain_count=0, next_attempt_at=now,
                lease_owner="", last_error="", created_at=now,
            ), ["event_id", "recipient_key"])
        return list(await session.scalars(select(Delivery.id).where(
            Delivery.event_id.in_(events), Delivery.recipient_key == recipient).order_by(Delivery.id)))
