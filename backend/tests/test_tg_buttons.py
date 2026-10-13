"""Кнопки под алертами в Telegram и команды бота."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alerts, collector, settings_store, tg_bot, tg_buttons
from app.models import Check, Server, TgAlert, User


class _Resp:
    def __init__(self, data, code=200):
        self._data, self.status_code = data, code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeTG:
    calls: list = []
    next_id = 100
    updates: dict = {"ok": True, "result": []}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        method = url.rsplit("/", 1)[1]
        FakeTG.calls.append((method, json))
        if method == "sendMessage":
            FakeTG.next_id += 1
            return _Resp({"ok": True, "result": {"message_id": FakeTG.next_id}})
        if method == "getUpdates":
            return _Resp(FakeTG.updates)
        if method == "getMe":
            return _Resp({"ok": True, "result": {"username": "kxbot"}})
        return _Resp({"ok": True, "result": True})

    @classmethod
    def of(cls, method):
        return [j for m, j in cls.calls if m == method]


@pytest.fixture
def tg(monkeypatch):
    FakeTG.calls, FakeTG.next_id = [], 100
    FakeTG.updates = {"ok": True, "result": []}
    monkeypatch.setattr("app.alerts.httpx.AsyncClient", FakeTG)
    return FakeTG


CFG = {"telegram_token": "T", "telegram_chat": "-100", "telegram_api": "", "webhook": "",
       "tg_buttons": True, "tz": "Europe/Moscow", "flood_threshold": 6}
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # 15:00 по Москве


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _server(factory, **kw) -> int:
    async with factory() as s:
        srv = Server(name=kw.pop("name", "web-a"), token_hash="x", enabled=True, **kw)
        s.add(srv)
        await s.commit()
        return srv.id


async def _row(factory, ref, kind, chat="-100") -> int:
    async with factory() as s:
        row = TgAlert(ref=ref, kind=kind, target="web-a", text="🔴 web-a — диск 92%",
                      messages=[{"chat": chat, "id": 7}])
        s.add(row)
        await s.commit()
        return row.id


async def _press(factory, aid, op, frm=None, chat="-100"):
    cq = {"id": "q1", "data": f"kx:{aid}:{op}", "from": frm or {"id": 555, "username": "petya"},
          "message": {"chat": {"id": int(chat)}, "message_id": 7}}
    await tg_bot.handle_callback(factory, CFG, cq, NOW)


# --- отправка -------------------------------------------------------------------------------

async def test_fire_gets_buttons_recovery_does_not(client, tg):
    """Срабатывание сервера уходит с кнопками и записью, куда ушло; отбой - без кнопок."""
    f = _factory(client)
    sid = await _server(f)
    fire = collector._server_alert_text("disk", "web-a", "занят на 92%", ref=f"srv:{sid}")
    rec = collector._server_alert_text("disk", "web-a", "снова в норме", recovery=True,
                                       ref=f"srv:{sid}")
    assert await alerts.dispatch(CFG, True, [fire, rec], 6, parse_mode="HTML", session_factory=f)
    sends = tg.of("sendMessage")
    assert len(sends) == 2
    kb = sends[0]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in kb[0]] == ["🔕 1 ч", "🔕 до утра", "🔕 насовсем"]
    assert kb[1][0]["text"] == "✋ Взял"
    assert "reply_markup" not in sends[1]
    async with f() as s:
        rows = (await s.scalars(select(TgAlert))).all()
    assert len(rows) == 1 and rows[0].ref == f"srv:{sid}" and rows[0].kind == "disk"
    assert rows[0].messages == [{"chat": "-100", "id": 101}]
    assert kb[0][0]["callback_data"] == f"kx:{rows[0].id}:h1"


async def test_no_buttons_when_disabled(client, tg):
    f = _factory(client)
    fire = collector._server_alert_text("disk", "web-a", "92%", ref="srv:1")
    await alerts.dispatch({**CFG, "tg_buttons": False}, True, [fire], 6, session_factory=f)
    assert "reply_markup" not in tg.of("sendMessage")[0]
    async with f() as s:
        assert (await s.scalars(select(TgAlert))).all() == []


async def test_digest_has_no_buttons(client, tg):
    f = _factory(client)
    msgs = [collector._server_alert_text("disk", f"web-{i}", "92%", ref=f"srv:{i}") for i in range(3)]
    await alerts.dispatch(CFG, True, msgs, 2, session_factory=f)
    assert len(tg.of("sendMessage")) == 1 and "reply_markup" not in tg.of("sendMessage")[0]


async def test_personal_copy_gets_same_buttons(client, tg):
    """Личная копия через общего бота - с теми же кнопками, и ее тоже потом поправят."""
    f = _factory(client)
    async with f() as s:
        s.add(User(username="ivan", password_hash="x", role="operator", tg_chat_id="777",
                   tg_alerts=True))
        await s.commit()
    fire = collector._server_alert_text("disk", "web-a", "92%", ref="srv:1")
    await alerts.dispatch(CFG, True, [fire], 6, session_factory=f)
    chats = {j["chat_id"]: j for j in tg.of("sendMessage")}
    assert "reply_markup" in chats["777"] and "reply_markup" in chats["-100"]
    async with f() as s:
        row = (await s.scalars(select(TgAlert))).one()
    assert {m["chat"] for m in row.messages} == {"777", "-100"}


# --- нажатия ---------------------------------------------------------------------------------

async def test_hour_snooze_and_undo(client, tg):
    f = _factory(client)
    sid = await _server(f)
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "h1")
    async with f() as s:
        srv = await s.get(Server, sid)
        row = await s.get(TgAlert, aid)
    until = datetime.fromisoformat(srv.alert_snoozes["disk"])
    assert until == NOW + timedelta(hours=1)
    assert row.muted == "🔕 @petya: заглушено на 1 ч, до 16:00"
    edit = tg.of("editMessageText")[-1]
    assert "заглушено на 1 ч" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["text"] == "↩️ Вернуть алерты"
    assert tg.of("answerCallbackQuery")[-1]["text"] == "Заглушено на 1 ч"

    await _press(f, aid, "un")
    async with f() as s:
        srv = await s.get(Server, sid)
        row = await s.get(TgAlert, aid)
    assert "disk" not in (srv.alert_snoozes or {})
    assert row.muted.startswith("↩️ @petya вернул алерты")
    assert tg.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "🔕 1 ч"


async def test_forever_mute_and_undo_keeps_others(client, tg):
    f = _factory(client)
    sid = await _server(f, alert_mutes=["cpu"])
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "pm")
    async with f() as s:
        assert (await s.get(Server, sid)).alert_mutes == ["cpu", "disk"]
    await _press(f, aid, "un")
    async with f() as s:
        assert (await s.get(Server, sid)).alert_mutes == ["cpu"]


async def test_undo_does_not_drop_mute_set_by_hand(client, tg):
    """Вид уже был заглушен насовсем в панели: "вернуть" после кнопки его не снимает."""
    f = _factory(client)
    sid = await _server(f, alert_mutes=["disk"])
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "pm")
    await _press(f, aid, "un")
    async with f() as s:
        assert (await s.get(Server, sid)).alert_mutes == ["disk"]


async def test_morning_is_9am_panel_time(client, tg):
    assert tg_buttons.next_morning(NOW, "Europe/Moscow") == datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    early = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)  # 06:00 МСК - еще сегодня
    assert tg_buttons.next_morning(early, "Europe/Moscow") == datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    assert tg_buttons.next_morning(NOW, "Нет/Такого").hour == 9  # кривой пояс - UTC


async def test_ack_once_and_status_line(client, tg):
    f = _factory(client)
    sid = await _server(f)
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "ak")
    await _press(f, aid, "ak", frm={"id": 1, "first_name": "Вася"})
    async with f() as s:
        row = await s.get(TgAlert, aid)
    assert row.acked_by == "@petya"
    assert tg.of("answerCallbackQuery")[-1]["text"] == "Уже взял @petya"
    edit = tg.of("editMessageText")[-1]
    assert "✋ взял @petya в 15:00" in edit["text"]
    assert all(b["text"] != "✋ Взял" for r in edit["reply_markup"]["inline_keyboard"] for b in r)


async def test_linked_viewer_can_only_ack(client, tg):
    f = _factory(client)
    async with f() as s:
        s.add(User(username="guest", password_hash="x", role="viewer", tg_chat_id="555"))
        await s.commit()
    sid = await _server(f)
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "pm")
    assert tg.of("answerCallbackQuery")[-1]["text"] == "Нужны права оператора"
    async with f() as s:
        assert not (await s.get(Server, sid)).alert_mutes
    await _press(f, aid, "ak")
    async with f() as s:
        assert (await s.get(TgAlert, aid)).acked_by == "guest"


async def test_this_is_us_acks_access_change(client, tg):
    f = _factory(client)
    sid = await _server(f, changes={"feed": []})
    aid = await _row(f, f"srv:{sid}", "access_change")
    await _press(f, aid, "us")
    async with f() as s:
        srv = await s.get(Server, sid)
    assert srv.changes["ack_by"] == "@petya" and srv.changes["ack_ts"] == NOW.isoformat()
    edit = tg.of("editMessageText")[-1]
    assert "✅ @petya: это мы" in edit["text"]
    assert all(b["text"] != "✅ Это мы" for r in edit["reply_markup"]["inline_keyboard"] for b in r)


async def test_press_from_foreign_chat_is_ignored(client, tg):
    f = _factory(client)
    sid = await _server(f)
    aid = await _row(f, f"srv:{sid}", "disk")
    await _press(f, aid, "pm", chat="-999")
    assert tg.of("answerCallbackQuery")[-1]["text"] == "Алерт устарел"
    async with f() as s:
        assert not (await s.get(Server, sid)).alert_mutes


async def test_monitor_buttons_cover_folded_monitors(client, tg):
    """Сообщение про домен с тремя мониторами: насовсем глушит все три, до утра - снуз всем."""
    f = _factory(client)
    async with f() as s:
        cs = [Check(name=f"m{i}", type="http", target=f"https://s{i}.example.com") for i in range(2)]
        s.add_all(cs)
        await s.commit()
        ids = [c.id for c in cs]
    aid = await _row(f, "chk:" + ",".join(map(str, ids)), "domain")
    await _press(f, aid, "pm")
    async with f() as s:
        for i in ids:
            assert "domain" in (await s.get(Check, i)).alert_mutes
    await _press(f, aid, "un")
    await _press(f, aid, "mo")
    async with f() as s:
        for i in ids:
            c = await s.get(Check, i)
            assert not c.alert_mutes
            snz = c.snooze_until if c.snooze_until.tzinfo else c.snooze_until.replace(tzinfo=timezone.utc)
            assert snz == datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


async def test_site_alert_carries_monitor_ref(client, monkeypatch, tg):
    """Сайтовый алерт несет ref монитора, а свернутые в него мониторы домена дописаны к нему."""
    from app.config import Settings

    f = _factory(client)
    async with f() as s:
        a = Check(name="a", type="http", target="https://a.example.com/")
        b = Check(name="b", type="http", target="https://b.example.com/")
        s.add_all([a, b])
        await s.commit()
        ia, ib = a.id, b.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    pending = [("domain", "a", "", "истекает через 7 дн.", None, ia, None),
               ("domain", "b", "", "истекает через 7 дн.", None, ib, None),
               ("recovery", "a", "up", "ok", None, ia, None)]
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"), pending, NOW)
    dom = [m for m in sent if m.kind == "domain"]
    assert len(dom) == 1 and dom[0].ref == f"chk:{ia},{ib}"
    assert all(not tg_buttons.wants_buttons(m) for m in sent if m.recovery)


# --- команды ---------------------------------------------------------------------------------

async def test_status_and_mutes_commands(client, tg):
    f = _factory(client)
    soon = (NOW + timedelta(hours=2)).isoformat()
    await _server(f, name="db-a", alert_state={"disk": 2, "units": 1, "cpu": 0},
                  alert_snoozes={"units": soon}, alert_mutes=["mem", "unit:foo.service"])
    await _server(f, name="ok-a", alert_state={"cpu": 0})
    async with f() as s:
        s.add(Check(name="shop", type="http", target="https://shop.example.com", last_status="down"))
        await s.commit()
        txt = await tg_bot.status_text(s, None, NOW)
        mut = await tg_bot.mutes_text(s, None, NOW, "Europe/Moscow")
    assert txt.startswith("🔴 Горит: серверов 1, сайтов 1")
    assert "<b>db-a</b>: Диск, 🔕 Systemd: упал юнит" in txt and "ok-a" not in txt
    assert "shop" in txt
    assert "<b>db-a</b>: Systemd: упал юнит до 17:00; насовсем: RAM; точечно: 1" in mut


async def test_command_only_in_known_chats(client, tg):
    f = _factory(client)
    msg = {"text": "/status@kxbot", "chat": {"id": -100}}
    await tg_bot.handle_message(f, CFG, msg, NOW)
    assert tg.of("sendMessage")[-1]["chat_id"] == "-100"
    n = len(tg.of("sendMessage"))
    await tg_bot.handle_message(f, CFG, {"text": "/status", "chat": {"id": 42}}, NOW)
    assert len(tg.of("sendMessage")) == n  # чужому чату не отвечаем


async def test_link_code_handled_by_bot(client, tg):
    f = _factory(client)
    async with f() as s:
        s.add(User(username="ivan", password_hash="x", role="operator", tg_link_code="ABC123"))
        await s.commit()
    await tg_bot.handle_message(f, CFG, {"text": "/link abc123", "chat": {"id": 31337}}, NOW)
    async with f() as s:
        u = await s.scalar(select(User).where(User.username == "ivan"))
    assert u.tg_chat_id == "31337" and u.tg_link_code == ""
    assert "Привязано к учетке ivan" in tg.of("sendMessage")[-1]["text"]


# --- опрос -----------------------------------------------------------------------------------

async def _set_cfg(f, on=True):
    async with f() as s:
        await settings_store.set_alert_config(s, "T", "-100", "", "", 6, on, "Europe/Moscow")


async def test_poll_off_by_default(client, tg):
    f = _factory(client)
    async with f() as s:
        await settings_store.set_alert_config(s, "T", "-100", "", "", 6)
    assert await tg_bot.poll_once(f, collector.get_settings()) == tg_bot._IDLE
    assert tg.of("getUpdates") == []


async def test_first_poll_skips_backlog_then_handles(client, tg):
    f = _factory(client)
    await _set_cfg(f)
    tg.updates = {"ok": True, "result": [{"update_id": 50, "message": {"text": "/status", "chat": {"id": -100}}}]}
    await tg_bot.poll_once(f, collector.get_settings())
    assert tg.of("getUpdates")[0]["offset"] == -1
    assert tg.of("sendMessage") == []  # старое в очереди не разбираем
    async with f() as s:
        st = await settings_store.get_tg_poll(s)
    assert st["offset"] == 51 and st["status"] == "ok" and st["bot"] == "@kxbot"

    tg.updates = {"ok": True, "result": [{"update_id": 51, "message": {"text": "/help", "chat": {"id": -100}}}]}
    await tg_bot.poll_once(f, collector.get_settings())
    assert tg.of("getUpdates")[-1]["offset"] == 51
    assert "Команды" in tg.of("sendMessage")[-1]["text"]
    async with f() as s:
        assert (await settings_store.get_tg_poll(s))["offset"] == 52


async def test_poll_conflict_shown_and_backs_off(client, tg):
    f = _factory(client)
    await _set_cfg(f)
    tg.updates = {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
    assert await tg_bot.poll_once(f, collector.get_settings()) == tg_bot._BACKOFF_CONFLICT
    async with f() as s:
        st = await settings_store.get_tg_poll(s)
    assert st["status"] == "conflict" and "Conflict" in st["error"]


# --- API -------------------------------------------------------------------------------------

async def test_api_saves_buttons_and_checks_tz(client, auth_headers, tg):
    body = {"telegram_token": "T", "telegram_chat": "-100", "telegram_api": "", "webhook": "",
            "flood_threshold": 6, "muted": False, "tg_buttons": True, "tz": "Мир/Нигде"}
    r = await client.put("/api/alerts", json=body, headers=auth_headers)
    assert r.status_code == 400
    r = await client.put("/api/alerts", json={**body, "tz": "Asia/Almaty"}, headers=auth_headers)
    assert r.status_code == 200
    j = r.json()
    assert j["tg_buttons"] is True and j["tz"] == "Asia/Almaty" and j["tg_poll"]["status"] == "off"


async def test_link_confirm_while_bot_polls(client, auth_headers, tg):
    """Пока панель сама читает общего бота, "Проверить привязку" не дергает getUpdates."""
    f = _factory(client)
    await _set_cfg(f)
    r = await client.post("/api/auth/telegram/link", headers=auth_headers)
    assert r.status_code == 200
    r = await client.post("/api/auth/telegram/confirm", headers=auth_headers)
    assert r.status_code == 404 and "еще не дошел" in r.json()["detail"]
    async with f() as s:
        u = await s.scalar(select(User).where(User.username == "admin"))
        u.tg_chat_id, u.tg_link_code = "31337", ""
        await s.commit()
    r = await client.post("/api/auth/telegram/confirm", headers=auth_headers)
    assert r.status_code == 200 and r.json()["chat_id"] == "31337"
