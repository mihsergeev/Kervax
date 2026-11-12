"""Мониторы одного сервера или адреса - одним сообщением (app/site_group.py): сбой соседа в
пределах получаса дописывается в отправленное, отбой - один, когда в норме все."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alerts, collector, parents, settings_store, site_flap, site_group
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Check, Server

T0 = datetime(2026, 10, 25, 18, 47, tzinfo=timezone.utc)
TLS = "соединение есть, но TLS-рукопожатие висит: 10 с без ответа"
SLOW = "сервер принял запрос, но не ответил за 10 с"
REFUSED = "сервер не принимает соединение: 10 с без ответа"
ADDR = {"a.example.com": "203.0.113.10", "api.example.org": "203.0.113.10", "shop.example.net": "203.0.113.10",
        "www.example.com": "203.0.113.10", "b.example.com": "203.0.113.10",
        "x.example.edu": "203.0.113.20"}


async def _panel(tmp_path, monkeypatch, names, servers=()):
    db = (tmp_path / "g.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ids = {}
    async with factory() as s:
        for srv in servers:
            s.add(srv)
        for n in names:
            c = Check(name=n, type="http", target=f"https://{n}/", enabled=True, interval_seconds=200)
            s.add(c)
            await s.flush()
            ids[n] = c.id
        await s.commit()

    async def addrs(host):
        return frozenset({ADDR[host]}) if host in ADDR else frozenset()

    monkeypatch.setattr(parents, "_lookup", addrs)
    sent, edits = [], []

    async def fake_send(cfg, text, parse_mode=None, reply_markup=None, out=None):
        sent.append(text)
        if out is not None:
            out.append({"chat": "100", "id": len(sent)})
        return []

    async def fake_edit(cfg, chat, message_id, text, reply_markup):
        edits.append((message_id, text))

    monkeypatch.setattr(alerts, "send_alert", fake_send)
    monkeypatch.setattr(alerts, "tg_edit", fake_edit)
    return engine, factory, ids, sent, edits


SET = Settings(alert_telegram_token="fake-token-example", alert_telegram_chat="100",
               panel_url="https://panel.example")


def _flaky(name, cid, now, err=TLS):
    return collector.Pending("flaky", name, "down", f"отвечает через раз: 4 из 20 последних проверок не прошли, "
                             f"последняя ошибка: {err}", None, cid,
                             [("flaky_notified", True), ("flap_state", site_flap.opened("flaky", now))], "")


def _back(name, cid):
    return collector.Pending("flakyrec", name, "up", "снова отвечает стабильно", None, cid,
                             ("flaky_notified", False), "")


def _plain(text):
    return re.sub(r"<[^>]+>", "", text)


def _down(name, cid, now, err=REFUSED):
    return collector.Pending("bad", name, "down", err, None, cid, ("flap_state", site_flap.opened("down", now)), "")


async def _recover(factory, cid, now):
    """Монитор пришел в норму: эпизод закрыт (как в record_outcome), отбой в очереди."""
    async with factory() as s:
        row = await s.get(Check, cid)
        row.flap_state = site_flap.closed(now)
        row.last_status = "up"
        await s.commit()


async def _history(factory):
    async with factory() as s:
        return [(e.kind, e.target, e.recovery) for e in
                (await s.scalars(select(AlertEvent).order_by(AlertEvent.id))).all()]


@pytest.mark.parametrize("failed_target", ["a.example.com", "x.example.edu"])
async def test_partial_delivery_retries_only_the_undelivered_recovery(tmp_path, monkeypatch, failed_target):
    names = ["a.example.com", "api.example.org", "x.example.edu"]
    engine, factory, ids, sent, _ = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(factory, SET, [_down(name, ids[name], T0) for name in names], T0)
    assert len(sent) == 2

    now = T0 + timedelta(minutes=8)
    async with factory() as session:
        for name in names:
            row = await session.get(Check, ids[name])
            row.last_status = "up"
            collector._queue_site_recovery(row, [], "recstable", "снова отвечает стабильно", now)
        await session.commit()

    original_send = alerts.send_alert

    async def fail_one(cfg, message, *args):
        if failed_target in message:
            return ["Telegram: temporarily unavailable"]
        return await original_send(cfg, message, *args)

    sent.clear()
    monkeypatch.setattr(alerts, "send_alert", fail_one)
    await collector._send_alerts(factory, SET, [], now)
    assert len(sent) == 1
    assert failed_target not in sent[0]

    async with factory() as session:
        queued, _ = await collector._site_recoveries(session, now)
    expected = names[:2] if failed_target == names[0] else names[2:]
    assert sorted(p.name for p in queued) == sorted(expected)

    monkeypatch.setattr(alerts, "send_alert", original_send)
    await collector._send_alerts(factory, SET, [], now + timedelta(minutes=1))
    await collector._send_alerts(factory, SET, [], now + timedelta(minutes=2))
    assert len(sent) == 2
    assert failed_target in sent[1]
    assert sorted(target for _, target, recovery in await _history(factory) if recovery) == sorted(names)
    await engine.dispose()


async def test_neighbours_join_the_sent_message_and_recover_together(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org", "shop.example.net", "x.example.edu"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    a, api, shop, x = (ids[n] for n in names)

    # первый - обычным сообщением
    await collector._send_alerts(f, SET, [_flaky("a.example.com", a, T0)], T0)
    assert len(sent) == 1 and "Отвечает через раз" in sent[0] and "a.example.com" in sent[0]

    # через 7 минут еще двое на том же адресе: нового сообщения нет, отправленное поправлено
    t1 = T0 + timedelta(minutes=7)
    await collector._send_alerts(f, SET, [_flaky("api.example.org", api, t1),
                                          _flaky("shop.example.net", shop, t1, SLOW)], t1)
    assert len(sent) == 1
    assert len(edits) == 1 and edits[0][0] == 1
    text = edits[0][1]
    assert text.startswith("🟠🌐 203.0.113.10\n<b>3 монитора отвечают через раз</b>\n")
    # Both reasons and the linked monitors remain visible in the compact summary.
    assert _plain(text).split("\n")[2:] == [
        "TLS висит 10 с; Не ответил за 10 с", "a.example.com, api.example.org, shop.example.net"]
    assert "https://panel.example/?check=" in text
    # у каждого монитора своя строка в истории
    assert [t for _k, t, _r in await _history(f)] == ["a.example.com", "api.example.org", "shop.example.net"]

    # другой адрес - свое сообщение
    t2 = T0 + timedelta(minutes=10)
    await collector._send_alerts(f, SET, [_flaky("x.example.edu", x, t2)], t2)
    assert len(sent) == 2 and "x.example.edu" in sent[1]

    # первый поднялся: отбоя пока нет, двое еще через раз
    t3 = T0 + timedelta(minutes=70)
    await _recover(f, a, t3)
    await collector._send_alerts(f, SET, [_back("a.example.com", a)], t3)
    assert len(sent) == 2

    # поднялись все: один отбой на троих, в истории строка на каждого
    t4 = T0 + timedelta(minutes=80)
    for c in (api, shop):
        await _recover(f, c, t4)
    await collector._send_alerts(f, SET, [_back("api.example.org", api), _back("shop.example.net", shop)], t4)
    assert len(sent) == 3
    assert sent[2].startswith("✅ 203.0.113.10: снова отвечают стабильно (3 монитора)\n")
    assert "a.example.com" in sent[2] and "shop.example.net" in sent[2]
    back = [(k, t) for k, t, r in await _history(f) if r]
    assert sorted(t for _k, t in back) == ["a.example.com", "api.example.org", "shop.example.net"]
    assert {k for k, _t in back} == {"flaky"}
    async with f() as s:
        left = await site_group.load(s)
    # Still unresolved: a later failure at the same place must join this message.
    assert len(left) == 1
    assert set(next(iter(left.values()))["members"]) == {str(x)}
    await engine.dispose()


async def test_same_tick_on_one_address_is_one_message(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_flaky(n, ids[n], T0) for n in names], T0)
    assert len(sent) == 1, sent
    assert "<b>2 монитора отвечают через раз</b>" in sent[0]
    assert len(await _history(f)) == 2
    await engine.dispose()


async def test_unresolved_episode_keeps_one_message_after_join_window(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_flaky("a.example.com", ids["a.example.com"], T0)], T0)
    late = T0 + site_group.JOIN + timedelta(minutes=1)
    await collector._send_alerts(f, SET, [_flaky("api.example.org", ids["api.example.org"], late)], late)
    assert len(sent) == 1 and len(edits) == 1
    recovered = late + timedelta(minutes=2)
    for name in names:
        await _recover(f, ids[name], recovered)
    await collector._send_alerts(f, SET, [_back(n, ids[n]) for n in names], recovered)
    assert len(sent) == 2 and "(2 монитора)" in sent[1]
    await engine.dispose()


async def test_unknown_keeps_group_open_and_cannot_confirm_recovery(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org"]
    engine, factory, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(factory, SET, [_down(names[0], ids[names[0]], T0)], T0)
    async with factory() as session:
        (await session.get(Check, ids[names[0]])).last_status = "unknown"
        await session.commit()
    late = T0 + timedelta(minutes=38)
    await collector._send_alerts(factory, SET, [_down(names[1], ids[names[1]], late)], late)
    assert len(sent) == 1 and len(edits) == 1
    await _recover(factory, ids[names[1]], late)
    await collector._send_alerts(factory, SET, [collector.Pending(
        "recstable", names[1], "up", "снова отвечает", None, ids[names[1]], None)], late)
    assert len(sent) == 1
    await _recover(factory, ids[names[0]], late + timedelta(minutes=1))
    await collector._send_alerts(factory, SET, [collector.Pending(
        "recstable", names[0], "up", "снова отвечает", None, ids[names[0]], None)], late + timedelta(minutes=1))
    assert len(sent) == 2 and "(2 монитора)" in sent[1]
    await engine.dispose()


async def test_retired_members_do_not_send_queued_failures_or_fake_recoveries(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org"]
    engine, factory, ids, sent, _ = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(factory, SET, [_down(n, ids[n], T0) for n in names], T0)
    async with factory() as session:
        for cid in ids.values():
            (await session.get(Check, cid)).enabled = False
        await session.commit()
    await collector._send_alerts(factory, SET, [_down(n, ids[n], T0) for n in names], T0)
    assert len(sent) == 1
    async with factory() as session:
        assert await site_group.load(session) == {}
    assert not [event for event in await _history(factory) if event[2]]
    await engine.dispose()


def test_unresolved_group_accepts_late_member_and_survives_unknown():
    now = T0 + timedelta(minutes=38)
    group = site_group.new_group("srv:1", "down", "node", 1, T0)
    group.update(grp="visible", members={"10": {"name": "first"}}, solo=True)
    groups = {group["id"]: group}
    # Неполученное измерение не закрывает уже объявленный down-эпизод.
    active = {10}
    assert site_group.open_for(groups, "srv:1", "down", "visible", now, active) is group
    assert not site_group.stale(group, now, active)
    assert site_group.open_for(groups, "srv:1", "down", "visible", now, set()) is None
    assert site_group.stale(group, now, set())


async def test_www_of_the_same_site_keeps_the_domain_fold(tmp_path, monkeypatch):
    """Сайт и его www в одном тике - как было: одно сообщение про сайт и строка "Еще"."""
    names = ["a.example.com", "www.example.com"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    pend = [collector.Pending("bad", n, "down", SLOW, None, ids[n],
                              ("flap_state", site_flap.opened("down", T0)), "") for n in names]
    await collector._send_alerts(f, SET, pend, T0)
    assert len(sent) == 1 and "Не отвечает" in sent[0] and "Еще: www.example.com" in sent[0]
    await engine.dispose()


@pytest.mark.parametrize(("kind", "second", "across_ticks", "rule_scope"), [
    ("bad", "www.example.com", False, False),
    ("domain", "www.example.com", False, False),
    ("bad", "api.example.org", False, False),
    ("bad", "api.example.org", True, False),
    ("bad", "www.example.com", False, True),
    ("domain", "www.example.com", False, True),
])
async def test_site_folding_preserves_route_and_rule_scope(
        tmp_path, monkeypatch, kind, second, across_ticks, rule_scope):
    first = "a.example.com"
    engine, f, ids, _, edits = await _panel(tmp_path, monkeypatch, [first, second])
    async with f() as s:
        (await s.get(Check, ids[first])).group_name = "team-a"
        (await s.get(Check, ids[second])).group_name = "team-a" if rule_scope else "team-b"
        if rule_scope:
            await settings_store.set_site_alert_rules(
                s, {collector._SITE_RULE_KIND[kind]: {"scope_type": "checks", "scope": [ids[first]]}},
            )
        await s.commit()
    delivered = []

    async def capture(_cfg, _can_send, messages, *_args, **_kwargs):
        delivered.extend(messages)
        for message in messages:
            if getattr(message, "sent_to", None) is not None:
                message.sent_to.append({"chat": "100", "id": len(delivered)})
        return True

    monkeypatch.setattr(alerts, "dispatch", capture)
    pending = [_down(name, ids[name], T0) if kind == "bad" else
               collector.Pending("domain", name, "up", "домен истекает", None, ids[name], None)
               for name in [first, second]]
    if across_ticks:
        await collector._send_alerts(f, SET, pending[:1], T0)
        await collector._send_alerts(f, SET, pending[1:], T0 + timedelta(minutes=1))
    else:
        await collector._send_alerts(f, SET, pending, T0)
    expected = [(first, "team-a", f"chk:{ids[first]}")]
    if not rule_scope:
        expected.append((second, "team-b", f"chk:{ids[second]}"))
    assert [(m.target, m.group, m.ref) for m in delivered] == expected
    assert second not in delivered[0] and second not in delivered[0].full
    assert not edits
    if kind == "bad" and rule_scope:
        async with f() as s:
            assert site_flap.level((await s.get(Check, ids[second])).flap_state) is None
    await engine.dispose()


async def test_old_group_does_not_repeat_another_groups_monitor(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org"]
    engine, f, ids, sent, _ = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_flaky(n, ids[n], T0) for n in names], T0)
    async with f() as s:
        groups = await site_group.load(s)
        group = next(iter(groups.values()))
        group.pop("group_scoped", None)
        await site_group.save(s, groups)
        (await s.get(Check, ids[names[1]])).group_name = "team-b"
        await s.commit()
    t1 = T0 + timedelta(minutes=1)
    for name in names:
        await _recover(f, ids[name], t1)
    await collector._send_alerts(f, SET, [_back(n, ids[n]) for n in names], t1)
    assert len(sent) == 3
    assert names[1] not in sent[1] and names[0] not in sent[2]
    await engine.dispose()


async def test_part_recovered_long_ago_gets_its_own_recovery(tmp_path, monkeypatch):
    names = ["a.example.com", "api.example.org", "shop.example.net"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_flaky(n, ids[n], T0) for n in names], T0)
    t1 = T0 + timedelta(minutes=40)
    await _recover(f, ids["a.example.com"], t1)
    await collector._send_alerts(f, SET, [_back("a.example.com", ids["a.example.com"])], t1)
    assert len(sent) == 1
    # прошел час, двое все еще через раз: отбой поднявшемуся, остальные ждут
    t2 = t1 + site_group.PARTIAL
    await collector._send_alerts(f, SET, [], t2)
    assert len(sent) == 2
    assert sent[1] == ("✅ 203.0.113.10: снова отвечает стабильно a.example.com\n"
                       "Еще нет: api.example.org, shop.example.net - TLS висит 10 с")
    t3 = t2 + timedelta(minutes=5)
    for n in names[1:]:
        await _recover(f, ids[n], t3)
    await collector._send_alerts(f, SET, [_back(n, ids[n]) for n in names[1:]], t3)
    assert len(sent) == 3 and "(2 монитора)" in sent[2]
    assert sorted(t for _k, t, r in await _history(f) if r) == sorted(names)
    await engine.dispose()


async def test_server_of_the_monitors_and_its_own_alarm(tmp_path, monkeypatch):
    """Мониторы на сервере панели: шапка - сервер ссылкой, его тревога - строкой."""
    srv = Server(name="web-a", token_hash="t-web-a", enabled=True, last_seen=T0, external_ip="203.0.113.30",
                 last_report={"web_services": [{"sites": ["a.example.com", "api.example.org"]}]},
                 alert_state={"cpu": 1, "disk_val": 90}, offline_after_seconds=120)
    names = ["a.example.com", "api.example.org"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names, [srv])
    monkeypatch.setattr(parents, "_lookup", lambda host: _none())
    await collector._send_alerts(f, SET, [_flaky(n, ids[n], T0) for n in names], T0)
    assert len(sent) == 1
    assert sent[0].startswith('🟠🌐 <a href="https://panel.example/?server=1">web-a</a>\n')
    assert "На сервере тоже: CPU" in sent[0]
    await engine.dispose()


async def _none():
    return frozenset()


async def test_server_silent_holds_monitors_as_before(tmp_path, monkeypatch):
    srv = Server(name="web-a", token_hash="t-web-a", enabled=True, last_seen=T0 - timedelta(minutes=5),
                 last_report={"web_services": [{"sites": ["a.example.com", "api.example.org"]}]},
                 offline_after_seconds=120)
    names = ["a.example.com", "api.example.org"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names, [srv])
    # T0 в прошлом относительно старта процесса: без этого панель "только запустилась" и не придерживает
    monkeypatch.setattr(collector, "panel_just_started", lambda now: False)
    await collector._send_alerts(f, SET, [_flaky(n, ids[n], T0) for n in names], T0)
    assert sent == []
    await engine.dispose()


def test_short_why():
    assert site_group.short_why(TLS) == "TLS висит 10 с"
    assert site_group.short_why(SLOW) == "не ответил за 10 с"
    assert site_group.short_why("сервер отклонил соединение: порт закрыт") == "соединение отклонено"
    assert site_group.short_why("HTTP 502") == "HTTP 502"
    assert len(site_group.short_why("x" * 300)) <= site_group.WHY_MAX


async def test_one_reason_for_all_is_written_once(tmp_path, monkeypatch):
    """26.10: пять мониторов за одним адресом, у всех одно и то же - причина строкой один раз.
    Двое свернуты по домену с первым (тот же сайт в одном тике) - и у них причина есть."""
    names = ["a.example.com", "b.example.com", "www.example.com", "api.example.org", "shop.example.net"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_down(n, ids[n], T0) for n in names], T0)
    assert len(sent) == 1, sent
    assert _plain(sent[0]).split("\n") == [
        "🔴🌐 203.0.113.10", "5 мониторов не отвечают", "Сервер не принимает соединение: 10 с без ответа",
        "a.example.com, b.example.com, www.example.com и еще 2"]
    assert 'shop.example.net' in alerts.full_text(sent[0])
    assert " - " not in sent[0]
    assert len(await _history(f)) == 5
    await engine.dispose()


async def test_folded_neighbour_keeps_its_own_reason(tmp_path, monkeypatch):
    """Свернутый по домену монитор в группе места - со своей причиной, а не без нее."""
    names = ["a.example.com", "b.example.com", "api.example.org"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    pend = [_down("a.example.com", ids["a.example.com"], T0), _down("b.example.com", ids["b.example.com"], T0, SLOW),
            _down("api.example.org", ids["api.example.org"], T0, SLOW)]
    await collector._send_alerts(f, SET, pend, T0)
    assert len(sent) == 1
    assert _plain(sent[0]).split("\n")[2:] == [
        "Сервер не принимает соединение: 10 с без ответа; Не ответил за 10 с",
        "a.example.com, b.example.com, api.example.org"]
    await engine.dispose()


async def test_edit_regroups_by_reason(tmp_path, monkeypatch):
    """Сосед доехал позже с другой причиной: в правке общая строка распадается на две."""
    names = ["a.example.com", "api.example.org", "shop.example.net"]
    engine, f, ids, sent, edits = await _panel(tmp_path, monkeypatch, names)
    await collector._send_alerts(f, SET, [_down(n, ids[n], T0) for n in names[:2]], T0)
    assert _plain(sent[0]).split("\n")[2:] == [
        "Сервер не принимает соединение: 10 с без ответа", "a.example.com, api.example.org"]
    t1 = T0 + timedelta(minutes=5)
    await collector._send_alerts(f, SET, [_down("shop.example.net", ids["shop.example.net"], t1, TLS)], t1)
    assert len(sent) == 1 and len(edits) == 1
    assert _plain(edits[0][1]).split("\n")[1:] == [
        "3 монитора не отвечают", "Сервер не принимает соединение: 10 с без ответа; TLS висит 10 с",
        "a.example.com, api.example.org, shop.example.net"]
    await engine.dispose()


def test_back_text_left_by_reason():
    g = {"lvl": "down", "label": "203.0.113.10"}
    left = [{"host": "api.example.org", "why": "TLS висит 10 с"}, {"host": "shop.example.net", "why": "HTTP 502"},
            {"host": "x.example.net", "why": "TLS висит 10 с"}, {"host": "y.example.net", "why": ""}]
    assert site_group.back_text(g, "", ["a.example.com"], left).split("\n")[1:] == [
        "Еще нет:", "api.example.org, x.example.net - TLS висит 10 с", "shop.example.net - HTTP 502", "y.example.net"]


def test_list_max_counts_across_reasons(monkeypatch):
    monkeypatch.setattr(site_group, "LIST_MAX", 3)
    mem = {str(i): {"host": f"h{i}.example.com", "why": "HTTP 502" if i < 2 else "HTTP 503"} for i in range(5)}
    text = site_group.alert_text({"lvl": "down", "label": "203.0.113.10", "members": mem}, "")[0]
    assert _plain(text).split("\n")[2:] == [
        "HTTP 502; HTTP 503", "h0.example.com, h1.example.com, h2.example.com и еще 2"]
