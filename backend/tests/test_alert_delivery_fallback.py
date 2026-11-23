"""Synthetic transport fixtures only: permanent group rejection can queue main."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app import alert_delivery, alert_delivery_fallback as fallback, alerts, notification_store as store
from app.db import Base, create_engine_and_factory
from app.models import NotificationDelivery as Delivery, NotificationEvent as Event, Server

pytestmark = pytest.mark.pg

NOW = datetime(2026, 11, 15, 0, tzinfo=timezone.utc)
CFG = {"telegram_api": "https://telegram.example.test", "telegram_token": "fixture-token",
       "telegram_chat": "main-example", "webhook": "", "tg_buttons": False,
       "routes": {"main": "rest", "chats": [{"chat": "group-example", "server_groups": ["example"],
                                                 "site_groups": [], "name": "Example group"}]}}


@pytest.fixture
async def db(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'fallback.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Server(name="Example", token_hash="fixture-source", enabled=True, group_name="example"))
        await session.commit()
    yield factory
    await engine.dispose()


async def source(factory, status="rejected", route="group", include_main=False):
    key, transport = alert_delivery._telegram_identity(CFG["telegram_api"], CFG["telegram_token"], "group-example")
    destinations = [{"key": key, "transport": transport,
                     "target": {"type": "telegram", "route": route, "bot": "common",
                                "chat": "group-example", "required": route != "personal"}}]
    if include_main:
        key, transport = alert_delivery._telegram_identity(CFG["telegram_api"], CFG["telegram_token"], CFG["telegram_chat"])
        destinations.append({"key": key, "transport": transport,
                             "target": {"type": "telegram", "route": "main", "bot": "common",
                                        "chat": CFG["telegram_chat"], "required": True}})
    message = alerts.Msg("Example queue incident", "servers", "example", kind="queue", target="Example",
                         ref="srv:1")
    spec = {"stream": "example:queue", "phase": "fire", "level": 1, "members": ["queue:example"],
            "observed_at": NOW, "payload": {"message": alert_delivery.pack(message), "parse_mode": "HTML",
                                            "threshold": 6}, "recipients": destinations}
    event_id = (await store.admit(factory, [spec], NOW))[0]
    async with factory() as session:
        rows = list(await session.scalars(select(Delivery).where(Delivery.event_id == event_id).order_by(Delivery.id)))
        rows[0].status = status
        rows[0].uncertain_count = int(status == "uncertain")
        if include_main:
            rows[1].status, rows[1].message_id = "confirmed", 71
        await session.commit()
        return rows[0].id, event_id, spec


async def test_enqueue_is_atomic_idempotent_and_stores_no_credentials(db):
    row_id, event_id, _ = await source(db)
    outputs = await asyncio.gather(*(fallback.enqueue(db, CFG, [{"id": row_id}], NOW) for _ in range(5)))
    assert all(len(ids) == 1 for ids in outputs) and len({ids[0] for ids in outputs}) == 1
    async with db() as session:
        rows = list(await session.scalars(select(Delivery).order_by(Delivery.id)))
        assert len(rows) == 2 and rows[0].status == "rejected"
        assert rows[1].event_id == event_id and rows[1].status == "pending"
        assert rows[1].target == {"type": "telegram", "route": "fallback", "bot": "common",
                                  "chat": CFG["telegram_chat"], "required": True, "reason": "group_rejected"}
        assert CFG["telegram_token"] not in repr(rows[1].__dict__)
        assert CFG["telegram_api"] not in repr(rows[1].__dict__)
        assert await session.scalar(select(func.count()).select_from(Event)) == 1


@pytest.mark.parametrize("state,route", [("uncertain", "group"), ("pending", "group"),
                                        ("sending", "group"), ("rejected", "personal")])
async def test_unknown_outcomes_and_personal_recipients_do_not_fallback(db, state, route):
    row_id, _, _ = await source(db, state, route)
    assert await fallback.enqueue(db, CFG, [{"id": row_id}], NOW) == []
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 1


async def test_existing_main_success_is_never_requeued(db):
    row_id, _, _ = await source(db, include_main=True)
    assert len(await fallback.enqueue(db, CFG, [{"id": row_id}], NOW)) == 1
    async with db() as session:
        rows = list(await session.scalars(select(Delivery).order_by(Delivery.id)))
        assert len(rows) == 2 and rows[1].status == "confirmed" and rows[1].message_id == 71


def mock_http(monkeypatch, *, ambiguous=False):
    calls = []
    real_client = httpx.AsyncClient

    async def handler(request):
        import json
        body = json.loads(request.content)
        calls.append(body["chat_id"])
        if body["chat_id"] == "group-example":
            if ambiguous:
                raise httpx.ReadTimeout("synthetic lost response", request=request)
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was kicked"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 72}})

    def client(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls


async def test_group_403_then_next_drain_main_success_never_duplicates(db, monkeypatch):
    row_id, event_id, spec = await source(db, "pending")
    calls = mock_http(monkeypatch)
    await alert_delivery.drain(db, CFG, NOW)
    # Also exercise the helper directly; an integrated worker call is idempotent.
    await fallback.enqueue(db, CFG, [{"id": row_id}], NOW)
    assert calls == ["group-example"]
    await alert_delivery.drain(db, CFG, NOW + timedelta(seconds=1))
    assert calls == ["group-example", "main-example"]
    spec["observed_at"] = NOW + timedelta(seconds=2)
    assert await store.admit(db, [spec], NOW + timedelta(seconds=2)) == [event_id]
    await fallback.enqueue(db, CFG, [{"id": row_id}], NOW + timedelta(seconds=2))
    await alert_delivery.drain(db, CFG, NOW + timedelta(seconds=3))
    assert calls == ["group-example", "main-example"]


async def test_ambiguous_timeout_does_not_enqueue_main(db, monkeypatch):
    row_id, _, _ = await source(db, "pending")
    calls = mock_http(monkeypatch, ambiguous=True)
    await alert_delivery.drain(db, CFG, NOW)
    assert await fallback.enqueue(db, CFG, [{"id": row_id}], NOW) == []
    await alert_delivery.drain(db, CFG, NOW + timedelta(minutes=1))
    assert calls == ["group-example"]
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 1
        assert (await session.get(Delivery, row_id)).status == "uncertain"


async def test_main_change_rejects_obsolete_fallback_destination(db, monkeypatch):
    row_id, _, _ = await source(db)
    ids = await fallback.enqueue(db, CFG, [{"id": row_id}], NOW)
    calls = mock_http(monkeypatch)
    await alert_delivery.drain(db, {**CFG, "telegram_chat": "different-main-example"}, NOW)
    assert calls == []
    async with db() as session:
        assert (await session.get(Delivery, ids[0])).status == "rejected"


def gated_http(monkeypatch, entered, release):
    calls = []
    real_client = httpx.AsyncClient

    async def handler(request):
        import json
        body = json.loads(request.content)
        calls.append(body["chat_id"])
        if body["chat_id"] == "group-example":
            entered.set()
            await release.wait()
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was kicked"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 73}})

    def client(*args, **kwargs):
        return real_client(*args, **{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls


async def test_group_403_while_the_caller_is_cancelled_still_falls_back_once(db, monkeypatch):
    # бота отменили при выкладке посреди POST в группу, группа ответила 403: отказ и копия в основной
    # чат пишутся вместе, копия уходит один раз
    row_id, event_id, _ = await source(db, "pending")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = gated_http(monkeypatch, entered, release)
    task = asyncio.create_task(alert_delivery.drain(db, CFG, NOW))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await alert_delivery.settle(5)
    async with db() as session:
        rows = list(await session.scalars(select(Delivery).order_by(Delivery.id)))
    assert [(row.status, row.target["route"]) for row in rows] == [("rejected", "group"), ("pending", "fallback")]
    assert rows[1].event_id == event_id
    await alert_delivery.drain(db, CFG, NOW + timedelta(seconds=1))
    await alert_delivery.drain(db, CFG, NOW + timedelta(seconds=2))
    assert calls == ["group-example", "main-example"]


async def test_lost_fallback_after_a_rejection_is_restored_once(db, monkeypatch):
    # процесс упал между записью отказа группы и постановкой копии: следующий проход ставит копию
    await source(db, "pending")
    calls = mock_http(monkeypatch)
    enqueue = fallback.enqueue
    lost = []

    async def crash(*args, **kwargs):
        if not lost:
            lost.append(True)
            raise RuntimeError("synthetic crash before the fallback row")
        return await enqueue(*args, **kwargs)

    monkeypatch.setattr(fallback, "enqueue", crash)
    await alert_delivery.drain(db, CFG, NOW)
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 1
    for second in (1, 2, 3):
        await alert_delivery.drain(db, CFG, NOW + timedelta(seconds=second))
    assert calls == ["group-example", "main-example"]
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 2
