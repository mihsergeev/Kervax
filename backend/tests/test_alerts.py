from app import alerts


class _FakeResp:
    def raise_for_status(self):
        pass


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.captured = {"url": url, "json": json}
        return _FakeResp()


async def test_send_alert_uses_custom_api(monkeypatch):
    monkeypatch.setattr("app.alerts.httpx.AsyncClient", _FakeClient)
    cfg = {
        "telegram_token": "TOK",
        "telegram_chat": "CHAT",
        "telegram_api": "https://api-tg.example.com/",  # хвостовой слэш срезается
        "webhook": "",
    }
    errors = await alerts.send_alert(cfg, "привет")
    assert errors == []
    assert _FakeClient.captured["url"] == "https://api-tg.example.com/botTOK/sendMessage"
    assert _FakeClient.captured["json"]["chat_id"] == "CHAT"


async def test_send_alert_defaults_to_telegram_org(monkeypatch):
    monkeypatch.setattr("app.alerts.httpx.AsyncClient", _FakeClient)
    cfg = {"telegram_token": "T", "telegram_chat": "C", "telegram_api": "", "webhook": ""}
    await alerts.send_alert(cfg, "x")
    assert _FakeClient.captured["url"] == "https://api.telegram.org/botT/sendMessage"


async def test_alert_coverage_finds_silent_objects(client, auth_headers):
    """Объект вне области всех правил не даст ни одного алерта — и это надо сказать.

    Достаточно один раз ограничить правила группой, чтобы всё заведённое вне её
    молчало. Молчание мониторинга не видно вообще: метрики идут, графики
    рисуются, а когда нода ляжет — не придёт ничего.
    """
    from sqlalchemy import select
    from app.models import Check, Server

    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as s:
        s.add(Server(name="в-группе", token_hash="a", enabled=True, group_name="VDNH"))
        s.add(Server(name="без-группы", token_hash="b", enabled=True, group_name=""))
        await s.commit()
    r = await client.post("/api/checks", headers=auth_headers,
                          json={"name": "сайт-без-группы", "type": "http",
                                "target": "https://x.example.ru"})
    assert r.status_code == 201

    # пока правила действуют на всё — немых нет
    cov = await client.get("/api/alerts/coverage", headers=auth_headers)
    assert cov.json()["items"] == []

    # ограничиваем всё группой VDNH
    rules = (await client.get("/api/alerts/server-rules", headers=auth_headers)).json()["rules"]
    for v in rules.values():
        v["scope_type"], v["scope"] = "groups", ["VDNH"]
    await client.put("/api/alerts/server-rules", headers=auth_headers, json={"rules": rules})
    srules = (await client.get("/api/alerts/site-rules", headers=auth_headers)).json()["rules"]
    for v in srules.values():
        v["scope_type"], v["scope"] = "groups", ["VDNH"]
    await client.put("/api/alerts/site-rules", headers=auth_headers, json={"rules": srules})

    items = (await client.get("/api/alerts/coverage", headers=auth_headers)).json()["items"]
    names = {i["name"] for i in items}
    assert "без-группы" in names, "сервер вне области не отмечен"
    assert "сайт-без-группы" in names, "монитор вне области не отмечен"
    assert "в-группе" not in names, "объект В области помечен немым — ложная тревога"
    assert all(i["reason"] for i in items), "причина должна быть названа словами"
