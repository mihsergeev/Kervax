"""Admin-only delivery metadata and explicit enqueue receipts; never send messages."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import alerts, notification_store as store
from app.config import get_settings
from app.models import AlertEvent, AuditLog, NotificationDelivery, NotificationEvent, User
from app.security import create_access_token


async def _seed(client, statuses):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    specs = [dict(stream=f"example:{i}", phase="fire", level=1, members=[], observed_at=now,
                  payload={"message": {"kind": "queue", "target": f"Example {i}",
                                       "text": "private payload must not be returned",
                                       "full": "private history must not be returned"}},
                  recipients=[dict(key=f"recipient:{i}", transport=f"transport:{i}",
                                   target={"chat": "private-recipient", "user_id": 999})])
             for i in range(len(statuses))]
    await store.admit(factory, specs, now)
    async with factory() as session:
        rows = list(await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))
        for row, state in zip(rows, statuses, strict=True):
            row.status = state
            row.attempts = 3 if state == "attention" else 1
            row.uncertain_count = 3 if state == "attention" else int(state == "uncertain")
            row.message_id = 100 if state == "confirmed" else None
        await session.commit()
        return [row.id for row in rows]


async def _headers(client, role):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        user = User(username=f"example-{role}", password_hash="unused-test-hash", role=role,
                    sections=["sites"], server_groups=["example"], site_groups=["example"])
        session.add(user)
        await session.commit()
        token = create_access_token(user.username, get_settings().jwt_secret, 10, user_id=user.id)
    return {"Authorization": f"Bearer {token}"}


async def test_delivery_metadata_is_admin_only(client, auth_headers):
    ids = await _seed(client, ["attention"])
    assert (await client.get("/api/alerts/deliveries")).status_code == 401
    assert (await client.post("/api/alerts/deliveries/review", json={"ids": ids})).status_code == 401
    for role in ("viewer", "editor"):
        headers = await _headers(client, role)
        read = await client.get("/api/alerts/deliveries", headers=headers)
        assert read.status_code == 403 and "Example 0" not in read.text
        assert (await client.post("/api/alerts/deliveries/retry", headers=headers,
                                  json={"ids": ids, "acknowledge_uncertain": True})).status_code == 403
        assert (await client.post("/api/alerts/deliveries/review", headers=headers, json={"ids": ids})).status_code == 403
    assert (await client.get("/api/alerts/deliveries", headers=auth_headers)).status_code == 200


async def test_delivery_counts_safe_rows_and_bound(client, auth_headers):
    await _seed(client, ["attention", "uncertain", "rejected"] + ["confirmed"] * 52)
    result = await client.get("/api/alerts/deliveries", headers=auth_headers)
    assert result.status_code == 200
    data = result.json()
    assert data["bad_count"] == 3 and len(data["items"]) == 3
    assert data["counts"]["confirmed"] == 52
    assert "private payload" not in result.text and "private-recipient" not in result.text
    assert "transport_key" not in result.text and "user_id" not in result.text
    assert {row["status"] for row in data["items"]} == {"attention", "uncertain", "rejected"}
    assert all(row["occurred_at"].endswith("Z") or row["occurred_at"].endswith("+00:00") for row in data["items"])
    all_rows = (await client.get("/api/alerts/deliveries?state=all", headers=auth_headers)).json()
    assert len(all_rows["items"]) == 50 and all_rows["more"]
    summary = (await client.get("/api/alerts/deliveries?summary=true", headers=auth_headers)).json()
    assert summary["items"] == [] and summary["bad_count"] == 3


async def test_review_preserves_outcome_and_excludes_only_selected_attention(client, auth_headers, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Review must not send a message")

    monkeypatch.setattr(alerts, "tg_send", forbidden)
    monkeypatch.setattr(alerts, "send_alert", forbidden)
    reviewed, untouched, uncertain, rejected, sending, confirmed, leased = await _seed(
        client, ["attention", "attention", "uncertain", "rejected", "sending", "confirmed", "attention"])
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        row = await session.get(NotificationDelivery, reviewed)
        row.last_error = "ReadTimeout"
        next_attempt = row.next_attempt_at
        for job_id in (sending, leased):
            job = await session.get(NotificationDelivery, job_id)
            job.lease_owner = "test-owner"
            job.lease_until = datetime.now(timezone.utc) + timedelta(hours=1)
        await session.commit()

    before = (await client.get("/api/alerts/deliveries", headers=auth_headers)).json()
    assert {item["id"] for item in before["items"] if item["can_review"]} == {reviewed, untouched}
    url = "/api/alerts/deliveries/review"
    ids = [reviewed, reviewed, uncertain, rejected, sending, confirmed, leased, 999999]
    response = await client.post(url, headers=auth_headers, json={"ids": ids})
    assert response.status_code == 200
    assert response.json() == {"reviewed_ids": [reviewed], "skipped_ids": sorted(set(ids) - {reviewed})}
    second = await client.post(url, headers=auth_headers, json={"ids": [reviewed]})
    assert second.json() == {"reviewed_ids": [], "skipped_ids": [reviewed]}

    after = (await client.get("/api/alerts/deliveries", headers=auth_headers)).json()
    assert {item["id"] for item in after["items"]} == {untouched, uncertain, rejected, leased}
    assert after["counts"] == before["counts"] and after["bad_count"] == before["bad_count"] - 1
    summary = (await client.get("/api/alerts/deliveries?summary=true", headers=auth_headers)).json()
    assert summary["bad_count"] == after["bad_count"] and summary["items"] == []
    history = (await client.get("/api/alerts/deliveries?state=reviewed", headers=auth_headers)).json()
    item, = history["items"]
    assert item["id"] == reviewed and item["status"] == "attention" and item["uncertain_count"] == 3
    assert item["reviewed_by"] == "admin" and item["reviewed_at"]
    assert not item["can_retry"] and not item["can_review"]

    retry = await client.post("/api/alerts/deliveries/retry", headers=auth_headers,
                              json={"ids": [reviewed], "acknowledge_uncertain": True})
    assert retry.json()["queued_ids"] == []
    async with factory() as session:
        row = await session.get(NotificationDelivery, reviewed)
        event = await session.get(NotificationEvent, row.event_id)
        assert (row.status, row.attempts, row.uncertain_count, row.next_attempt_at) == ("attention", 3, 3, next_attempt)
        assert row.last_error == "ReadTimeout" and row.message_id is None and not event.history_recorded
        assert not await session.scalar(select(AlertEvent.id))
        log, = (await session.scalars(select(AuditLog).where(AuditLog.action == "alert_delivery_review"))).all()
        assert (log.username, log.detail) == ("admin", str(reviewed))
    assert reviewed not in {job["id"] for job in await store.due(factory, datetime.now(timezone.utc))}


async def test_review_and_retry_cannot_both_win(client, auth_headers):
    delivery, = await _seed(client, ["attention"])
    review, retry = await asyncio.gather(
        client.post("/api/alerts/deliveries/review", headers=auth_headers, json={"ids": [delivery]}),
        client.post("/api/alerts/deliveries/retry", headers=auth_headers,
                    json={"ids": [delivery], "acknowledge_uncertain": True}),
    )
    assert review.status_code == retry.status_code == 200
    reviewed = review.json()["reviewed_ids"]
    queued = retry.json()["queued_ids"]
    assert reviewed + queued == [delivery]
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        row = await session.get(NotificationDelivery, delivery)
        assert (row.reviewed_at is not None) == bool(reviewed)
        assert row.status == ("attention" if reviewed else "pending")


async def test_uncertain_retry_requires_confirmation_and_only_enqueues(client, auth_headers, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("An API retry must not send messages")

    monkeypatch.setattr(alerts, "tg_send", forbidden)
    monkeypatch.setattr(alerts, "send_alert", forbidden)
    ids = await _seed(client, ["uncertain", "confirmed"])
    denied = await client.post("/api/alerts/deliveries/retry", headers=auth_headers, json={"ids": [ids[0]]})
    assert denied.status_code == 409 and "копию" in denied.json()["detail"]
    queued = await client.post("/api/alerts/deliveries/retry", headers=auth_headers,
                              json={"ids": ids + [999999], "acknowledge_uncertain": True})
    assert queued.status_code == 200
    assert queued.json() == {"queued_ids": [ids[0]], "skipped_ids": [ids[1], 999999], "delivery_confirmed": False}
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        retried = await session.get(NotificationDelivery, ids[0])
        assert retried.status == "pending" and retried.attempts == retried.uncertain_count == 0
        assert (await session.get(NotificationDelivery, ids[1])).status == "confirmed"
        assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 0
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "alert_delivery_retry"))
        assert audit.username == "admin" and str(ids[0]) in audit.detail


@pytest.mark.parametrize("ids", [[], [0], [-1], [True], ["1"], list(range(1, 52))])
async def test_retry_rejects_invalid_or_oversized_ids(client, auth_headers, ids):
    result = await client.post("/api/alerts/deliveries/retry", headers=auth_headers, json={"ids": ids})
    assert result.status_code == 422


async def test_retry_rate_is_persisted_and_limited(client, auth_headers):
    ids = await _seed(client, ["rejected"])
    for _ in range(5):
        result = await client.post("/api/alerts/deliveries/retry", headers=auth_headers, json={"ids": ids})
        assert result.status_code == 200
    denied = await client.post("/api/alerts/deliveries/retry", headers=auth_headers, json={"ids": ids})
    assert denied.status_code == 429 and denied.headers["retry-after"] == "60"
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "alert_delivery_retry")) == 5
