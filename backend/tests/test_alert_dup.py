"""Копии в основной чат: по группам на выбор и пауза на время, пока смотрят чат группы.

Основной чат получает копии того, что ушло в чаты по группам. Иногда это нужно, а иногда -
дубль: человек сидит в чате группы и видит то же самое второй раз в личке. Копию можно
выключить по группе (main_by) или на время (пауза копий), критичное при этом не теряется."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import alerts, collector, settings_store, tg_bot
from app.config import get_settings
from app.models import Server, TgAlert
from tests.test_alert_routes import ROUTE, FakeTG, _cfg, _factory, _site, _srv, tg  # noqa: F401

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _paused(until: str = "", **kw):
    return _cfg(dup_pause={"on": True, "until": until, "by": "admin", "at": ""}, **kw)


def _offline(name, group, ref="srv:1", recovery=False):
    if recovery:
        return collector._server_alert_text("offline", name, "снова на связи", recovery=True, group=group, ref=ref)
    return collector._server_alert_text("offline", name, "агент не шлет метрики", group=group, ref=ref)


def test_main_by_group():
    """Копия по группе: выключена у одной группы - остальные как main; в main=rest - включена."""
    cfg = _cfg("all")
    cfg["routes"]["main_by"] = {"srv:web": False}
    assert alerts.chats_for(cfg, _srv("web-a", "web")) == ["-200"]
    # сайты той же группы решаются отдельно
    assert alerts.chats_for(cfg, _site("shop", "shop")) == ["-100", "-200"]
    cfg = _cfg("rest")
    cfg["routes"]["main_by"] = {"site:shop": True}
    assert alerts.chats_for(cfg, _site("shop", "shop")) == ["-100", "-200"]
    assert alerts.chats_for(cfg, _srv("web-a", "web")) == ["-200"]
    # группа без своего чата всегда идет в основной: выключить там нечего
    cfg["routes"]["main_by"] = {"srv:infra": False}
    assert alerts.chats_for(cfg, _srv("infra-a", "infra")) == ["-100"]


def test_routes_clean_main_by():
    """Хранится только то, что отличается от main, и только понятные ключи."""
    r = settings_store._routes_clean({"main": "all", "chats": [ROUTE], "main_by": {
        "srv:web": False, "site:shop": True, "srv:": False, "bad:web": False, "web": False}})
    assert r["main_by"] == {"srv:web": False}
    r = settings_store._routes_clean({"main": "rest", "main_by": {"srv:web": True, "site:shop": False}})
    assert r["main_by"] == {"srv:web": True}


async def test_pause_keeps_main_for_the_rest(client, tg):
    """Пауза: в основной чат не идут копии групп со своим чатом, остальное идет как обычно."""
    msgs = [_srv("web-a", "web"), _srv("infra-a", "infra"), _site("shop", "shop"),
            alerts.Msg("⚠️ панель: общий алерт")]
    assert await alerts.dispatch(_paused(), True, msgs, 0, parse_mode="HTML")
    main = tg.to("-100")
    assert len(main) == 2 and "infra-a" in main[0] and "общий" in main[1]
    assert len(tg.to("-200")) == 2


def test_pause_ends_by_itself():
    """Время паузы вышло - копии вернулись без чьих-то действий."""
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert alerts.chats_for(_paused(past), _srv("web-a", "web")) == ["-100", "-200"]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert alerts.chats_for(_paused(future), _srv("web-a", "web")) == ["-200"]


def test_pause_value_and_text():
    v = alerts.dup_pause_value("2h", NOW, "Europe/Moscow", "admin")
    assert v["on"] and v["until"] == (NOW + timedelta(hours=2)).isoformat() and v["by"] == "admin"
    v = alerts.dup_pause_value("morning", NOW, "Europe/Moscow", "admin")
    assert datetime.fromisoformat(v["until"]) == datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    assert alerts.dup_pause_value("forever", NOW, "UTC", "admin")["until"] == ""
    assert alerts.dup_pause_value("off", NOW, "UTC", "admin") == {"on": False}
    cfg = _cfg(dup_pause=alerts.dup_pause_value("2h", NOW, "UTC", "admin"), tz="UTC")
    assert alerts.dup_pause_text(cfg, NOW) == "до 14:00"
    assert alerts.dup_pause_text({**cfg, "dup_pause": {"on": True, "until": ""}}, NOW) == "пока не включат"
    assert alerts.dup_pause_text(cfg, NOW + timedelta(hours=3)) == ""


def test_critical():
    assert alerts.critical(_offline("web-a", "web"))
    assert not alerts.critical(_offline("web-a", "web", recovery=True))
    assert alerts.critical(collector._server_alert_text("disk", "web-a", "занят на 99%", group="web", level=3))
    assert not alerts.critical(_srv("web-a", "web"))


async def test_pause_critical_without_buttons_goes_to_main(client, tg):
    """Без кнопок бота взять алерт в группе нечем: критичное в паузу идет в основной чат сразу,
    и его отбой - тоже, а обычный отбой группы - нет."""
    f = _factory(client)
    async with f() as s:
        await settings_store.set_dup_pause(s, {"on": True, "until": ""})
        cfg = await settings_store.get_alert_config(s, get_settings())
    cfg = {**cfg, "telegram_token": "T", "telegram_chat": "-100", "routes": {"main": "all", "chats": [ROUTE]}}
    assert await alerts.dispatch(cfg, True, [_offline("web-a", "web"), _srv("web-b", "web")], 0,
                                 parse_mode="HTML", session_factory=f)
    assert len(tg.to("-100")) == 1 and "web-a" in tg.to("-100")[0]
    assert len(tg.to("-200")) == 2
    async with f() as s:
        cfg = {**cfg, "main_passed": await settings_store.get_main_passed(s)}
    assert cfg["main_passed"] == [["offline", "web-a"]]
    rec = collector._server_alert_text("disk", "web-b", "ok", recovery=True, group="web")
    assert await alerts.dispatch(cfg, True, [_offline("web-a", "web", recovery=True), rec], 0,
                                 parse_mode="HTML", session_factory=f)
    assert len(tg.to("-100")) == 2 and "web-a" in tg.to("-100")[1]
    async with f() as s:
        assert await settings_store.get_main_passed(s) == []


async def test_pause_critical_with_buttons_waits_for_ack(client, tg, monkeypatch):
    """С кнопками бота критичное в паузу сначала только в чате группы. Никто не взял за
    DUP_HOLD_MIN - уходит в основной чат; взяли - не уходит."""
    f = _factory(client)
    async with f() as s:
        a = Server(name="web-a", token_hash="x", enabled=True, group_name="web", alert_state={"offline": 2})
        b = Server(name="web-b", token_hash="y", enabled=True, group_name="web", alert_state={"offline": 2})
        s.add_all([a, b])
        await s.commit()
        ids = (a.id, b.id)
    monkeypatch.setattr(settings_store, "get_alert_config", _fake_cfg)
    cfg = await _fake_cfg(None, None)
    msgs = [_offline("web-a", "web", f"srv:{ids[0]}"), _offline("web-b", "web", f"srv:{ids[1]}")]
    assert await alerts.dispatch(cfg, True, msgs, 0, parse_mode="HTML", session_factory=f)
    assert tg.to("-100") == [] and len(tg.to("-200")) == 2
    async with f() as s:
        rows = {r.target: r for r in (await s.scalars(select(TgAlert))).all()}
        assert all(r.undo.get("held") == "-100" for r in rows.values())
        rows["web-b"].acked_by = "@petya"
        await s.commit()
    now = datetime.now(timezone.utc)
    assert await tg_bot.dup_catchup(f, get_settings(), now + timedelta(minutes=5)) == 0
    assert await tg_bot.dup_catchup(f, get_settings(), now + timedelta(minutes=11)) == 1
    main = tg.to("-100")
    assert len(main) == 1 and "никто не взял" in main[0] and "web-a" in main[0]
    async with f() as s:
        assert all("held" not in (r.undo or {}) for r in (await s.scalars(select(TgAlert))).all())
        assert await settings_store.get_main_passed(s) == [["offline", "web-a"]]
    # второй заход ничего не повторяет
    assert await tg_bot.dup_catchup(f, get_settings(), now + timedelta(minutes=20)) == 0


async def _fake_cfg(session, settings):
    return {**_paused(tg_buttons=True), "main_passed": []}


async def test_api_dup_pause(client, auth_headers, tg):
    r = await client.post("/api/alerts/dup-pause", json={"op": "2h"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    p = r.json()["dup_pause"]
    assert p["on"] and p["until"] and p["by"] == "admin"
    r = await client.get("/api/alerts", headers=auth_headers)
    assert r.json()["dup_pause"]["on"]
    # сохранение формы паузу не трогает
    body = {"telegram_token": "T", "telegram_chat": "-100", "routes": {"main": "all", "chats": [ROUTE],
                                                                       "main_by": {"srv:web": False}}}
    r = await client.put("/api/alerts", json=body, headers=auth_headers)
    assert r.json()["dup_pause"]["on"] and r.json()["routes"]["main_by"] == {"srv:web": False}
    r = await client.post("/api/alerts/dup-pause", json={"op": "off"}, headers=auth_headers)
    assert r.json()["dup_pause"]["on"] is False
    r = await client.post("/api/alerts/dup-pause", json={"op": "week"}, headers=auth_headers)
    assert r.status_code == 422


async def test_bot_copies_command(client, tg):
    """/copies в основном чате: кнопки паузы; нажатие ставит паузу и меняет сообщение. В чате
    группы паузу не поставить."""
    f = _factory(client)
    cfg = _cfg(tg_buttons=True, dup_pause={"on": False})
    now = datetime.now(timezone.utc)
    await tg_bot.handle_message(f, cfg, {"text": "/copies", "chat": {"id": -100, "type": "private"},
                                         "from": {"id": 5}}, now)
    sent = [j for m, j in tg.calls if m == "sendMessage"][-1]
    assert sent["chat_id"] == "-100" and "kxc:2h" in str(sent["reply_markup"])
    cq = {"id": "q1", "data": "kxc:2h", "from": {"id": 5, "username": "boss"},
          "message": {"chat": {"id": -100, "type": "private"}, "message_id": 7}}
    await tg_bot.handle_callback(f, cfg, cq, now)
    async with f() as s:
        p = await settings_store.get_dup_pause(s)
    assert p["on"] and p["by"] == "@boss"
    edit = [j for m, j in tg.calls if m == "editMessageText"][-1]
    assert "не идут до" in edit["text"] and "kxc:off" in str(edit["reply_markup"])
    # из чата группы - отказ, пауза не меняется
    cq2 = {**cq, "data": "kxc:off", "message": {"chat": {"id": -200, "type": "supergroup"}, "message_id": 8}}
    await tg_bot.handle_callback(f, cfg, cq2, now)
    async with f() as s:
        assert (await settings_store.get_dup_pause(s))["on"]
    toast = [j for m, j in tg.calls if m == "answerCallbackQuery"][-1]
    assert "основного чата" in toast["text"]
