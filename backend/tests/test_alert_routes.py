"""Чаты по группам: алерты выбранных групп серверов и сайтов идут в свои чаты."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import alerts, collector, settings_store, tg_bot
from app.models import Check, Server, TgAlert


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
    broken: set = set()
    next_id = 100

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        method = url.rsplit("/", 1)[1]
        if method == "sendMessage" and str(json.get("chat_id")) in FakeTG.broken:
            return _Resp({"ok": False}, 400)
        FakeTG.calls.append((method, json))
        FakeTG.next_id += 1
        return _Resp({"ok": True, "result": {"message_id": FakeTG.next_id}})

    @classmethod
    def sent(cls) -> list[tuple[str, str]]:
        return [(str(j["chat_id"]), j["text"]) for m, j in cls.calls if m == "sendMessage"]

    @classmethod
    def to(cls, chat: str) -> list[str]:
        return [t for c, t in cls.sent() if c == chat]


@pytest.fixture
def tg(monkeypatch):
    FakeTG.calls, FakeTG.broken, FakeTG.next_id = [], set(), 100
    monkeypatch.setattr("app.alerts.httpx.AsyncClient", FakeTG)
    return FakeTG


ROUTE = {"name": "CS-deploy", "chat": "-200", "server_groups": ["web"], "site_groups": ["shop"]}


def _cfg(main="all", **kw):
    return {"telegram_token": "T", "telegram_chat": "-100", "telegram_api": "", "webhook": "",
            "tg_buttons": False, "tz": "Europe/Moscow", "flood_threshold": 6,
            "routes": {"main": main, "chats": [ROUTE]}, **kw}


def _srv(name, group, kind="disk"):
    return collector._server_alert_text(kind, name, "занят на 92%", group=group, ref="srv:1")


def _site(name, group):
    return alerts.Msg(f"🔴 {name} — недоступен", "sites", group, "bad", name)


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def test_group_alerts_go_to_their_chat(client, tg):
    """Основной чат получает все, чат по группам - только свои группы серверов и сайтов."""
    msgs = [_srv("web-a", "web"), _srv("infra-a", "infra"), _site("shop", "shop"),
            _site("blog", "blog"), alerts.Msg("⚠️ панель: общий алерт")]
    assert await alerts.dispatch(_cfg(), True, msgs, 0, parse_mode="HTML")
    assert len(tg.to("-100")) == 5
    route = tg.to("-200")
    assert len(route) == 2 and "web-a" in route[0] and "shop" in route[1]


async def test_main_gets_only_the_rest(client, tg):
    """main=rest: в основной чат - только то, что не ушло в чаты по группам."""
    msgs = [_srv("web-a", "web"), _srv("infra-a", "infra"), _srv("lone-a", ""), _site("shop", "shop")]
    assert await alerts.dispatch(_cfg("rest"), True, msgs, 0, parse_mode="HTML")
    main = tg.to("-100")
    assert len(main) == 2 and "infra-a" in main[0] and "lone-a" in main[1]
    assert len(tg.to("-200")) == 2


async def test_broken_route_falls_back_to_main_without_retry(client, tg):
    """Чат по группам не принял: сообщение в основном чате с припиской, повтора нет (иначе
    основной чат получал бы его каждый цикл), а ошибка видна в настройках."""
    f = _factory(client)
    tg.broken = {"-200"}
    ok = await alerts.dispatch(_cfg("rest"), True, [_srv("web-a", "web")], 0, parse_mode="HTML",
                               session_factory=f)
    assert ok
    main = tg.to("-100")
    assert len(main) == 1 and "web-a" in main[0] and "не дошло в чат «CS-deploy»" in main[0]
    async with f() as s:
        state = await settings_store.get_routes_state(s)
    assert "-200" in state and "HTTP 400" in state["-200"]["err"]
    # чат снова принимает - ошибка из настроек уходит
    tg.broken = set()
    assert await alerts.dispatch(_cfg("rest"), True, [_srv("web-a", "web")], 0, session_factory=f)
    async with f() as s:
        assert await settings_store.get_routes_state(s) == {}


async def test_broken_route_no_second_copy_when_main_has_it(client, tg):
    tg.broken = {"-200"}
    assert await alerts.dispatch(_cfg("all"), True, [_srv("web-a", "web")], 0)
    assert len(tg.to("-100")) == 1 and "не дошло" not in tg.to("-100")[0]


async def test_broken_main_is_retried(client, tg):
    tg.broken = {"-100"}
    assert not await alerts.dispatch(_cfg("rest"), True, [_srv("infra-a", "infra")], 0)
    # то, что принял чат по группам, считается доставленным, но основной сбоит - повтор
    assert not await alerts.dispatch(_cfg("all"), True, [_srv("web-a", "web")], 0)
    assert len(tg.to("-200")) == 1


async def test_digest_per_chat(client, tg):
    msgs = [_srv(f"web-{i}", "web", k) for i, k in enumerate(("disk", "cpu", "mem"))] + [_srv("infra-a", "infra")]
    assert await alerts.dispatch(_cfg("rest"), True, msgs, 3, parse_mode="HTML")
    route = tg.to("-200")
    assert len(route) == 1 and route[0].startswith("🌊 3 алертов")
    main = tg.to("-100")
    assert len(main) == 1 and "infra-a" in main[0]


async def test_buttons_work_in_route_chat(client, tg):
    """Кнопки под алертом в чате по группам: отправка записана, нажатие оттуда принимается."""
    f = _factory(client)
    async with f() as s:
        srv = Server(name="web-a", token_hash="x", enabled=True, group_name="web")
        s.add(srv)
        await s.commit()
        sid = srv.id
    fire = collector._server_alert_text("disk", "web-a", "92%", group="web", ref=f"srv:{sid}")
    cfg = _cfg("rest", tg_buttons=True)
    assert await alerts.dispatch(cfg, True, [fire], 6, parse_mode="HTML", session_factory=f)
    async with f() as s:
        row = (await s.scalars(select(TgAlert))).one()
    assert [m["chat"] for m in row.messages] == ["-200"]
    cq = {"id": "q1", "data": f"kx:{row.id}:ak", "from": {"id": 555, "username": "petya"},
          "message": {"chat": {"id": -200}, "message_id": row.messages[0]["id"]}}
    await tg_bot.handle_callback(f, cfg, cq, datetime.now(timezone.utc))
    async with f() as s:
        row = await s.get(TgAlert, row.id)
    assert row.acked_by == "@petya"


async def test_bot_commands_in_route_chat(client, tg):
    """/status в чате по группам - только про его группы; /id отвечает в любом чате."""
    f = _factory(client)
    async with f() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, group_name="web",
                     alert_state={"disk": 2}))
        s.add(Server(name="infra-a", token_hash="y", enabled=True, group_name="infra",
                     alert_state={"disk": 2}))
        s.add(Check(name="blog", type="http", target="https://blog.example.com", last_status="down",
                    group_name="blog"))
        await s.commit()
    cfg = _cfg()
    now = datetime.now(timezone.utc)
    await tg_bot.handle_message(f, cfg, {"text": "/status", "chat": {"id": -200}}, now)
    txt = tg.to("-200")[-1]
    assert "web-a" in txt and "infra-a" not in txt and "blog" not in txt
    await tg_bot.handle_message(f, cfg, {"text": "/id@kxbot", "chat": {"id": -555}}, now)
    assert tg.to("-555")[-1] == "id этого чата: <code>-555</code>"


async def test_api_routes_roundtrip(client, auth_headers, tg):
    f = _factory(client)
    async with f() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, group_name="web"))
        s.add(Check(name="shop", type="http", target="https://shop.example.com", group_name="shop"))
        await s.commit()
    body = {"telegram_token": "T", "telegram_chat": "-100", "telegram_api": "", "webhook": "",
            "flood_threshold": 6, "muted": False, "tg_buttons": False, "tz": "",
            "routes": {"main": "rest", "chats": [
                {"name": " CS-deploy ", "chat": " -200 ", "server_groups": ["web", "web", " "],
                 "site_groups": ["shop"]},
                {"name": "empty", "chat": "  ", "server_groups": ["web"]}]}}
    r = await client.put("/api/alerts", json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["routes"] == {"main": "rest", "chats": [
        {"name": "CS-deploy", "chat": "-200", "server_groups": ["web"], "site_groups": ["shop"]}]}
    assert j["route_groups"] == {"servers": ["web"], "sites": ["shop"]}
    # без поля routes чаты по группам не трогаем
    r = await client.put("/api/alerts", json={k: v for k, v in body.items() if k != "routes"},
                         headers=auth_headers)
    assert r.json()["routes"]["chats"][0]["chat"] == "-200"
    r = await client.post("/api/alerts/routes/test", json={"chat": "-200"}, headers=auth_headers)
    assert r.json()["sent"] is True
    assert "групп web, shop" in tg.to("-200")[-1]
    tg.broken = {"-300"}
    r = await client.post("/api/alerts/routes/test", json={"chat": "-300"}, headers=auth_headers)
    assert r.json()["sent"] is False and "HTTP 400" in r.json()["errors"][0]


async def test_dispatch_reads_routes_from_settings(client, tg):
    """Коллектор берет конфиг из get_alert_config: чаты по группам должны в нем быть."""
    f = _factory(client)
    async with f() as s:
        await settings_store.set_routes(s, {"main": "rest", "chats": [ROUTE]})
        await s.commit()
        from app.config import get_settings
        cfg = await settings_store.get_alert_config(s, get_settings())
    assert cfg["routes"]["main"] == "rest" and cfg["routes"]["chats"][0]["chat"] == "-200"


def test_server_alerts_carry_group():
    """Отбой без группы уходил только в основной чат: в чат по группам и людям с доступом лишь к
    этой группе он не доходил, хотя сам алерт туда приходил. Каждый вызов _server_alert_text
    обязан передать группу сервера."""
    import inspect
    import re

    from app import collector

    src = inspect.getsource(collector)
    missing = []
    for m in re.finditer(r"_server_alert_text\(", src):
        if src[max(0, m.start() - 4):m.start()] == "def ":
            continue
        i, depth = m.end(), 1
        while depth:
            depth += src[i] == "("
            depth -= src[i] == ")"
            i += 1
        if "group=" not in src[m.start():i]:
            missing.append(src.count("\n", 0, m.start()) + 1)
    assert not missing, f"серверный алерт без группы, строки collector.py: {missing}"


def test_recovery_goes_to_group_chat():
    """Отбой серверного алерта с группой уходит в чат этой группы, как и сам алерт."""
    from app import collector

    cfg = _cfg("all")
    msg = collector._server_alert_text("kube_restarts", "web-a", "поды восстановлены", recovery=True, group="web")
    assert msg.group == "web" and msg.recovery
    assert "-200" in alerts.chats_for(cfg, msg)
