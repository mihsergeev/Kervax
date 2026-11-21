"""Explicit source changes fence queued POSTs without pretending they were delivered."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import alert_delivery, alerts, config, parents, tg_bot
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Check, NotificationDelivery, NotificationEvent, Server, TgAlert, User

pytestmark = pytest.mark.pg


@pytest.fixture
async def panel(tmp_path, monkeypatch):
    from app.bootstrap import ensure_admin
    from app.main import create_app

    pg = os.environ.get("KERVAX_TEST_PG", "")
    url = pg or f"sqlite+aiosqlite:///{(tmp_path / 'source-policy.db').as_posix()}"
    monkeypatch.setenv("KERVAX_DB_URL", url)
    monkeypatch.setenv("KERVAX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KERVAX_ADMIN_USER", "example-admin")
    monkeypatch.setenv("KERVAX_ADMIN_PASSWORD", "fixture-password-135")
    monkeypatch.setenv("KERVAX_JWT_SECRET", "fixture-secret-source-policy-135-000000")
    config.get_settings.cache_clear()
    app = create_app()
    async with app.state.engine.begin() as connection:
        if pg:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await ensure_admin(app.state.session_factory, config.get_settings())
    factory = app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        server = Server(name="example-node", token_hash="fixture", enabled=True, group_name="alpha",
                        alert_state={"disk": 2}, backup_not_required=True)
        neighbor = Server(name="example-neighbor", token_hash="fixture-neighbor", enabled=True,
                          group_name="alpha", backup_not_required=True)
        check = Check(name="example-monitor", type="http", target="https://monitor.example.test/",
                      enabled=True, group_name="alpha", last_status="down")
        session.add_all([server, neighbor, check])
        await session.commit()
        sid, neighbor_id, cid = server.id, neighbor.id, check.id

    calls, edits = [], []
    failures = {}

    async def send(cfg, text, *_args, **_kwargs):
        chat = cfg["telegram_chat"]
        calls.append({"chat": chat, "text": text})
        failure = failures.get(chat)
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture accepted POST response lost")
        if failure == "connect":
            raise httpx.ConnectError("fixture connection unavailable")
        result = alerts.SendErrors()
        result.receipts.append({"chat": chat, "id": 100 + len(calls)})
        return result

    async def no_edit(*args, **_kwargs):
        edits.append(args)

    async def no_call(*_args, **_kwargs):
        return {}

    async def no_dns(*_args, **_kwargs):
        return frozenset()

    monkeypatch.setattr(alerts, "send_alert", send)
    monkeypatch.setattr(alerts, "tg_edit", no_edit)
    monkeypatch.setattr(tg_bot, "_call", no_call)
    monkeypatch.setattr(parents, "_lookup", no_dns)
    monkeypatch.setattr(alert_delivery, "_monotonic", lambda: 0.0)
    cfg = {"telegram_token": "fixture-bot-token", "telegram_chat": "-100", "telegram_api": "",
           "webhook": "", "tg_buttons": False, "routes": {}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": "example-admin",
                                                          "password": "fixture-password-135"})
        assert login.status_code == 200, login.text
        p = SimpleNamespace(engine=app.state.engine, factory=factory, url=url, now=now, cfg=cfg,
                            sid=sid, neighbor_id=neighbor_id, cid=cid, client=client,
                            headers={"Authorization": "Bearer " + login.json()["access_token"]},
                            calls=calls, failures=failures)
        try:
            yield p
        finally:
            await p.engine.dispose()
            await app.state.engine.dispose()
            config.get_settings.cache_clear()


def message(p, source="server", *, sid=None, kind=None, at=None, phase="fire", keys=None,
            level=2, members=None, explicit=True):
    source_id = sid if sid is not None else p.sid if source == "server" else p.cid
    kind = kind or ("disk" if source == "server" else "down")
    at = at or p.now
    result = alerts.Msg("Example incident: alpha and beta facts", "servers" if source == "server" else "sites",
                        "alpha", kind=kind, target="example-node" if source == "server" else "example-monitor",
                        recovery=phase == "recovery", ref=f"{'srv' if source == 'server' else 'chk'}:{source_id}",
                        mutes=keys or [])
    result.notice = {"stream": f"{'server' if source == 'server' else 'check'}:{source_id}:{kind}",
                     "phase": phase, "observed_at": at, "level": level, "members": members or ["example"]}
    if explicit:
        result.notice["source_id" if source == "server" else "check_id"] = source_id
    return result


async def dispatch(p, item, at=None):
    return await alerts.dispatch(p.cfg, True, item if isinstance(item, list) else [item], 0,
                                 parse_mode="HTML", session_factory=p.factory, now=at or p.now)


async def jobs(p):
    async with p.factory() as session:
        return list(await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))


async def history(p):
    async with p.factory() as session:
        return list(await session.scalars(select(AlertEvent).order_by(AlertEvent.id)))


async def restart(p):
    await p.engine.dispose()
    p.engine, p.factory = create_engine_and_factory(p.url)


async def request(p, method, path, body=None):
    response = await p.client.request(method, path, json=body, headers=p.headers)
    assert response.status_code in (200, 204), response.text
    return response


@pytest.mark.parametrize("source", ["server", "check"])
@pytest.mark.parametrize("action", ["mute", "disable", "delete"])
@pytest.mark.parametrize("failure", ["connect", "timeout"])
async def test_explicit_source_change_cancels_queued_post_after_restart(panel, source, action, failure):
    p = panel
    p.failures["-100"] = failure
    assert not await dispatch(p, message(p, source))
    first = (await jobs(p))[0]
    assert first.status == ("uncertain" if failure == "timeout" else "pending")
    source_id = p.sid if source == "server" else p.cid
    path = f"/api/{'servers' if source == 'server' else 'checks'}/{source_id}"
    if action == "mute":
        if source == "server":
            await request(p, "POST", path + "/alert-mutes", {"add": ["disk"]})
        else:
            await request(p, "PATCH", path, {"alert_mutes": ["down"]})
    else:
        await request(p, "DELETE" if action == "delete" else "PATCH", path,
                      None if action == "delete" else {"enabled": False})
    await restart(p)
    p.failures.clear()
    later = p.now + timedelta(minutes=16)
    await alert_delivery.drain(p.factory, p.cfg, later)
    final = (await jobs(p))[0]
    assert len(p.calls) == 1 and final.status == "superseded" and final.message_id is None
    assert await history(p) == []
    # The policy of this source must not consume an independent neighbor's event.
    assert await dispatch(p, message(p, sid=p.neighbor_id, at=later), later)
    assert len(p.calls) == 2 and len(await history(p)) == 1


@pytest.mark.parametrize("source", ["server", "check"])
@pytest.mark.parametrize("failure", ["connect", "timeout"])
async def test_temporary_snooze_defers_without_spending_attempts_or_ambiguity(panel, source, failure):
    p = panel
    p.failures["-100"] = failure
    await dispatch(p, message(p, source))
    first = (await jobs(p))[0]
    identity = p.sid if source == "server" else p.cid
    path = f"/api/{'servers' if source == 'server' else 'checks'}/{identity}/snooze"
    await request(p, "POST", path, {"hours": 1})
    async with p.factory() as session:
        source_row = await session.get(Server if source == "server" else Check, identity)
        until = alert_delivery._utc(source_row.snooze_until)
    await restart(p)
    p.failures.clear()
    for minute in (16, 17, 18):
        await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(minutes=minute))
    held = (await jobs(p))[0]
    assert len(p.calls) == 1 and held.status == first.status
    assert (held.event_id, held.attempts, held.uncertain_count) == (
        first.event_id, first.attempts, first.uncertain_count)
    assert alert_delivery._utc(held.next_attempt_at) == until
    assert await history(p) == []
    await restart(p)
    await alert_delivery.drain(p.factory, p.cfg, until + timedelta(seconds=1))
    final = (await jobs(p))[0]
    assert len(p.calls) == 2 and final.status == "confirmed"
    assert final.event_id == first.event_id and final.attempts == first.attempts + 1
    assert final.uncertain_count == first.uncertain_count and len(await history(p)) == 1


async def test_partial_item_mute_and_new_severity_keep_unmuted_facts(panel):
    p = panel
    await request(p, "POST", f"/api/servers/{p.sid}/alert-mutes", {"add": ["unit:alpha", "disk@1"]})
    # A unit aggregate remains wanted while beta is not muted.
    assert await dispatch(p, message(p, kind="units", keys=["unit:alpha", "unit:beta"]))
    assert "beta" in p.calls[-1]["text"]
    warning = message(p, level=1, members=["disk|level:1"])
    await dispatch(p, warning)
    assert (await jobs(p))[-1].status == "superseded"
    later = p.now + timedelta(minutes=1)
    critical = message(p, level=2, members=["disk|level:2"], at=later)
    assert await dispatch(p, critical, later)
    assert len(p.calls) == 2 and len(await history(p)) == 2


@pytest.mark.parametrize("kind,key", [("unit_loop", "unit:alpha.service"), ("queue", "qstuck:broker|//alpha")])
async def test_per_instance_sources_honor_their_actual_item_keys(panel, kind, key):
    p = panel
    item = message(p, kind=kind)
    item.notice["instance"] = key.split(":", 1)[1]
    await alert_delivery.admit(p.factory, p.cfg, [item], 0, "HTML", p.now)
    await request(p, "POST", f"/api/servers/{p.sid}/alert-mutes", {"add": [key]})
    await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(seconds=1))
    assert p.calls == [] and (await jobs(p))[0].status == "superseded"


async def test_item_snooze_wakes_at_first_available_fact_and_preserves_neighbor_lease(panel):
    p = panel
    for key, hours in (("unit:alpha", 0.5), ("unit:beta", 1)):
        await request(p, "POST", f"/api/servers/{p.sid}/snooze-alert", {"kind": key, "hours": hours})
    async with p.factory() as session:
        row = await session.get(Server, p.sid)
        first_until = datetime.fromisoformat(row.alert_snoozes["unit:alpha"])
    first = message(p, kind="units", keys=["unit:alpha", "unit:beta"])
    neighbor = message(p, sid=p.neighbor_id)
    # Force both rows into one owned transport batch, with one deferred member.
    _, mapping = await alert_delivery.admit(p.factory, p.cfg, [first, neighbor], 0, "HTML", p.now)
    due = await alert_delivery.store.due(p.factory, p.now)
    await alert_delivery._post(p.factory, p.cfg, due, p.now)
    current = await jobs(p)
    held = next(job for job in current if job.event_id == mapping[0][0])
    sent = next(job for job in current if job.event_id == mapping[1][0])
    assert held.status == "pending" and held.attempts == 0 and held.uncertain_count == 0
    assert alert_delivery._utc(held.next_attempt_at) == first_until
    assert sent.status == "confirmed" and sent.attempts == 1
    assert len(p.calls) == 1 and len(await history(p)) == 1
    await alert_delivery.drain(p.factory, p.cfg, first_until + timedelta(seconds=1))
    assert len(p.calls) == 2 and all(job.status == "confirmed" for job in await jobs(p))
    assert len(set(event for events in mapping.values() for event in events)) == 2


@pytest.mark.parametrize("source", ["server", "check"])
async def test_recovery_still_delivers_under_source_snooze(panel, source):
    p = panel
    assert await dispatch(p, message(p, source))
    identity = p.sid if source == "server" else p.cid
    await request(p, "POST", f"/api/{'servers' if source == 'server' else 'checks'}/{identity}/snooze", {"hours": 20})
    later = p.now + timedelta(minutes=1)
    recovered = message(p, source, phase="recovery", level=0, at=later)
    recovered.recovery = False  # Some expiry producers use Event.phase as their authority.
    assert await dispatch(p, recovered, later)
    assert len(p.calls) == 2 and len(await history(p)) == 2
    assert (await history(p))[-1].recovery


async def acknowledge(p, aid, at):
    async with p.factory() as session:
        button = await session.get(TgAlert, aid)
        receipt = button.messages[0]
    await tg_bot.handle_callback(p.factory, p.cfg, {
        "id": "fixture-callback", "data": f"kx:{aid}:ak", "from": {"id": 123, "username": "example_operator"},
        "message": {"chat": {"id": int(receipt["chat"])}, "message_id": receipt["id"]},
    }, at)
    async with p.factory() as session:
        assert (await session.get(TgAlert, aid)).acked_by == "@example_operator"


@pytest.mark.parametrize("failure", ["connect", "timeout"])
async def test_verified_ack_stops_only_uncertain_recipient_and_new_facts_survive(panel, failure):
    p = panel
    p.cfg.update(tg_buttons=True, routes={"main": "all", "chats": [
        {"chat": "-200", "server_groups": ["alpha"]}]})
    p.failures["-200"] = failure
    await dispatch(p, message(p))
    initial = await jobs(p)
    async with p.factory() as session:
        event = await session.get(NotificationEvent, initial[0].event_id)
        aid = event.tg_alert_id
    await acknowledge(p, aid, p.now + timedelta(seconds=1))
    await restart(p)
    p.failures.clear()
    later = p.now + timedelta(minutes=16)
    await alert_delivery.drain(p.factory, p.cfg, later)
    final = await jobs(p)
    assert final[1].status == ("superseded" if failure == "timeout" else "confirmed")
    assert len(p.calls) == (2 if failure == "timeout" else 3)
    assert len(await history(p)) == 1
    fresh = message(p, at=later, members=["new-fact"])
    assert await dispatch(p, fresh, later)
    assert len(p.calls) == (4 if failure == "timeout" else 5)
    assert len(await history(p)) == 2


@pytest.mark.parametrize("ack_after_new_fact", [False, True])
async def test_old_ack_of_reused_button_does_not_cancel_new_uncertain_fact(panel, ack_after_new_fact):
    p = panel
    p.cfg["tg_buttons"] = True
    assert await dispatch(p, message(p))
    async with p.factory() as session:
        old = (await session.scalars(select(NotificationEvent))).one()
        aid = old.tg_alert_id
    if not ack_after_new_fact:
        await acknowledge(p, aid, p.now + timedelta(seconds=1))
    later = p.now + timedelta(minutes=1)
    fresh = message(p, at=later, members=["new-fact"])
    _, mapping = await alert_delivery.admit(p.factory, p.cfg, [fresh], 0, "HTML", later)
    await alert_delivery.store.set_tg_alert(p.factory, mapping[0][0], aid)
    p.failures["-100"] = "timeout"
    await alert_delivery.drain(p.factory, p.cfg, later)
    if ack_after_new_fact:
        # This callback belongs to the OLD known message, not the new fact's
        # uncertain POST. A late timestamp alone is not a delivery receipt.
        await acknowledge(p, aid, later + timedelta(seconds=1))
    p.failures.clear()
    await alert_delivery.drain(p.factory, p.cfg, later + timedelta(minutes=16))
    assert len(p.calls) == 3 and (await jobs(p))[-1].status == "confirmed"
    assert len(await history(p)) == 2


async def test_moved_source_cannot_send_to_old_group_or_add_new_recipient(panel):
    p = panel
    p.cfg["routes"] = {"main": "rest", "chats": [
        {"chat": "-200", "server_groups": ["alpha"]}, {"chat": "-300", "server_groups": ["beta"]}]}
    await alert_delivery.admit(p.factory, p.cfg, [message(p)], 0, "HTML", p.now)
    await request(p, "PATCH", f"/api/servers/{p.sid}", {"group_name": "beta"})
    await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(minutes=1))
    rows = await jobs(p)
    assert len(rows) == 1 and rows[0].status == "superseded" and p.calls == []
    assert await history(p) == []


async def test_generic_notices_and_legacy_unambiguous_source_refs(panel):
    p = panel
    plain = alerts.Msg("Example setup notice", kind="setup")
    assert await dispatch(p, plain)
    assert await dispatch(p, message(p, explicit=False))
    legacy = message(p, kind="cpu", explicit=False)
    await alert_delivery.admit(p.factory, p.cfg, [legacy], 0, "HTML", p.now)
    await request(p, "DELETE", f"/api/servers/{p.sid}")
    await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(minutes=1))
    assert len(p.calls) == 2 and (await jobs(p))[-1].status == "superseded"


async def test_partial_atom_does_not_leak_muted_fact_in_wire_or_restart_history(panel, monkeypatch):
    p = panel
    original = message(p, kind="units", keys=["unit:hidden-alpha", "unit:allowed-beta"])
    original_text = "Example raw private detail hidden-alpha and allowed-beta"
    original = alerts.Msg(original_text, original.section, original.group, original.kind, original.target,
                          ref=original.ref, full=original_text, line=original_text, mutes=original.mutes)
    original.notice = message(p, kind="units").notice
    await alert_delivery.admit(p.factory, p.cfg, [original], 0, "HTML", p.now)
    await request(p, "POST", f"/api/servers/{p.sid}/alert-mutes", {"add": ["unit:hidden-alpha"]})
    record = alert_delivery._history

    async def crash(*_args, **_kwargs):
        raise RuntimeError("fixture crash after confirmed filtered POST")

    monkeypatch.setattr(alert_delivery, "_history", crash)
    # сбой после подтвержденного POST пишется в лог и не выходит из доставки
    await alert_delivery.drain(p.factory, p.cfg, p.now)
    assert len(p.calls) == 1 and "allowed-beta" in str(p.calls[0]["text"])
    assert "hidden-alpha" not in str(p.calls[0]["text"])
    saved = (await jobs(p))[0]
    assert saved.status == "confirmed" and "policy_message" in saved.target
    assert "hidden-alpha" not in repr(saved.target["policy_message"])
    async with p.factory() as session:
        event = await session.get(NotificationEvent, saved.event_id)
        assert "hidden-alpha" in event.payload["message"]["full"]
    monkeypatch.setattr(alert_delivery, "_history", record)
    await restart(p)
    await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(minutes=1))
    assert len(p.calls) == 1
    recorded = await history(p)
    assert len(recorded) == 1 and "allowed-beta" in recorded[0].text and "hidden-alpha" not in recorded[0].text


@pytest.mark.parametrize("phase", ["fire", "recovery"])
async def test_disabled_fold_sibling_cannot_leak_through_shared_atomic_snapshot(panel, phase):
    p = panel
    async with p.factory() as session:
        second = Check(name="allowed-beta", type="http", target="https://allowed.example.test/",
                       enabled=True, group_name="alpha")
        session.add(second)
        await session.commit()
        second_id = second.id
    shared = "Shared snapshot hidden-alpha and allowed-beta"
    first = message(p, "check", kind="domain", members=["threshold:7"], phase=phase,
                    level=0 if phase == "recovery" else 2)
    first = alerts.Msg(shared, first.section, first.group, first.kind, first.target,
                       ref=first.ref, full=shared, line=shared)
    first.notice = message(p, "check", kind="domain", members=["threshold:7"], phase=phase,
                           level=0 if phase == "recovery" else 2).notice
    second = alerts.Msg(shared, "sites", "alpha", kind="domain", target="allowed-beta",
                        ref=f"chk:{second_id}", full=shared, line=shared)
    second.notice = {**first.notice, "check_id": second_id, "stream": f"check:{second_id}:domain"}
    parent = alerts.Msg(shared, "sites", "alpha", kind="domain", full=shared, line=shared)
    parent.parts = [first, second]
    await alert_delivery.admit(p.factory, p.cfg, [parent], 0, "HTML", p.now)
    await request(p, "PATCH", f"/api/checks/{p.cid}", {"enabled": False})
    await alert_delivery.drain(p.factory, p.cfg, p.now + timedelta(seconds=1))
    assert len(p.calls) == 1
    assert "allowed-beta" in str(p.calls[0]["text"]) and "hidden-alpha" not in str(p.calls[0]["text"])
    assert "Домен" in str(p.calls[0]["text"])
    assert str(p.calls[0]["text"]).startswith("✅" if phase == "recovery" else "🔴")
    assert sorted(job.status for job in await jobs(p)) == ["confirmed", "superseded"]
    assert len(await history(p)) == 1 and "hidden-alpha" not in (await history(p))[0].text


async def test_slow_snooze_policy_can_release_its_own_expired_no_post_lease(panel, monkeypatch):
    p = panel
    await alert_delivery.admit(p.factory, p.cfg, [message(p)], 0, "HTML", p.now)
    await request(p, "POST", f"/api/servers/{p.sid}/snooze", {"hours": 1})
    clock = [0.0]
    wanted = alert_delivery._wanted

    async def slow(*args):
        result = await wanted(*args)
        clock[0] += 121
        return result

    monkeypatch.setattr(alert_delivery, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(alert_delivery, "_wanted", slow)
    await alert_delivery.drain(p.factory, p.cfg, p.now)
    row = (await jobs(p))[0]
    assert p.calls == [] and row.status == "pending" and row.attempts == row.uncertain_count == 0
    assert not row.lease_owner and row.lease_until is None


async def test_defer_does_not_release_a_reclaimed_lease(panel):
    p = panel
    await alert_delivery.admit(p.factory, p.cfg, [message(p)], 0, "HTML", p.now)
    row = (await jobs(p))[0]
    store = alert_delivery.store
    assert await store.claim(p.factory, [row.id], "old-worker", p.now, lease_seconds=1)
    await store.due(p.factory, p.now + timedelta(seconds=2))
    later = p.now + timedelta(minutes=16)
    assert await store.claim(p.factory, [row.id], "new-worker", later)
    assert await store.defer(p.factory, [row.id], "old-worker", later, later + timedelta(hours=1)) == []
    owned = (await jobs(p))[0]
    assert owned.status == "sending" and owned.lease_owner == "new-worker"
    assert owned.attempts == 2 and owned.uncertain_count == 1


@pytest.mark.parametrize("swap_mute", [False, True])
async def test_retry_rechecks_canonical_facts_and_replaces_saved_snapshot(panel, monkeypatch, swap_mute):
    p = panel
    item = message(p, kind="units", keys=["unit:first", "unit:second"],
                   members=["unit:first", "unit:second"])
    await request(p, "POST", f"/api/servers/{p.sid}/alert-mutes", {"add": ["unit:second"]})
    p.failures["-100"] = "timeout"
    await dispatch(p, item)
    first = (await jobs(p))[0]
    assert first.status == "uncertain" and first.target["policy_message"]["mutes"] == ["unit:first"]
    await request(p, "POST", f"/api/servers/{p.sid}/alert-mutes",
                  {"remove": ["unit:second"], "add": ["unit:first"] if swap_mute else []})
    p.failures.clear()
    record = alert_delivery._history

    async def crash(*_args, **_kwargs):
        raise RuntimeError("fixture crash after updated-policy POST")

    monkeypatch.setattr(alert_delivery, "_history", crash)
    later = p.now + timedelta(minutes=16)
    # сбой после подтвержденного POST пишется в лог и не выходит из доставки
    await alert_delivery.drain(p.factory, p.cfg, later)
    assert len(p.calls) == 2
    saved = (await jobs(p))[0]
    assert saved.status == "confirmed"
    if swap_mute:
        assert saved.target["policy_message"]["mutes"] == ["unit:second"]
        assert "unit:second" in str(p.calls[-1]["text"]) and "unit:first" not in str(p.calls[-1]["text"])
    else:
        assert "policy_message" not in saved.target
        assert "alpha and beta" in str(p.calls[-1]["text"])
    monkeypatch.setattr(alert_delivery, "_history", record)
    await restart(p)
    await alert_delivery.drain(p.factory, p.cfg, later + timedelta(minutes=1))
    assert len(p.calls) == 2 and len(await history(p)) == 1
    text = (await history(p))[0].text
    assert ("unit:second" in text and "unit:first" not in text) if swap_mute else "alpha and beta" in text


async def test_personal_bot_message_id_collision_cannot_verify_common_event_ack(panel, monkeypatch):
    p = panel
    p.cfg["tg_buttons"] = True
    assert await dispatch(p, message(p))
    async with p.factory() as session:
        old = (await session.scalars(select(NotificationEvent))).one()
        aid = old.tg_alert_id
        button = await session.get(TgAlert, aid)
        receipt = button.messages[0]
        session.add(User(username="example-subscriber", password_hash="fixture", role="viewer",
                         sections=["servers"], server_groups=["alpha"], tg_chat_id=receipt["chat"],
                         tg_token="fixture-personal-bot", tg_alerts=True))
        await session.commit()
    send = alerts.send_alert

    async def different_bot(cfg, text, *args, **kwargs):
        if cfg["telegram_token"] != "fixture-personal-bot":
            return await send(cfg, text, *args, **kwargs)
        p.calls.append({"chat": cfg["telegram_chat"], "text": text})
        result = alerts.SendErrors()
        # Telegram ids are scoped to bot/chat; this is not the common bot's copy.
        result.receipts.append({"chat": receipt["chat"], "id": receipt["id"]})
        return result

    monkeypatch.setattr(alerts, "send_alert", different_bot)
    later = p.now + timedelta(minutes=1)
    fresh = message(p, at=later, members=["new-fact"])
    _, mapping = await alert_delivery.admit(p.factory, p.cfg, [fresh], 0, "HTML", later)
    event_id = mapping[0][0]
    await alert_delivery.store.set_tg_alert(p.factory, event_id, aid)
    p.failures["-100"] = "timeout"
    await alert_delivery.drain(p.factory, p.cfg, later)
    current = [job for job in await jobs(p) if job.event_id == event_id]
    assert {(job.target["bot"], job.status) for job in current} == {
        ("common", "uncertain"), ("personal", "confirmed")}
    await acknowledge(p, aid, later + timedelta(seconds=1))
    p.failures.clear()
    await restart(p)
    await alert_delivery.drain(p.factory, p.cfg, later + timedelta(minutes=16))
    final = [job for job in await jobs(p) if job.event_id == event_id]
    assert len(p.calls) == 4 and all(job.status == "confirmed" for job in final)
    assert len(await history(p)) == 2
