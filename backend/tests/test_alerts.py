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
    assert all(i["reason_code"] for i in items), "причина должна быть названа"
    # у объекта без группы дописывать в область по группам нечего — кнопки не будет
    assert not any(i["fixable"] for i in items), "нечинимое помечено чинимым"


async def test_alert_coverage_apply_widens_scope(client, auth_headers):
    """Кнопка «включить алерты» должна дописывать объект в область правил.

    Иначе немой объект чинится в другом месте и вручную: сообразить, что дело в
    области действия, открыть настройки и добавить группу в КАЖДЫЙ тип алертов.
    """
    from app.models import Server

    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as s:
        s.add(Server(name="в-группе", token_hash="c", enabled=True, group_name="VDNH"))
        s.add(Server(name="чужая-группа", token_hash="d", enabled=True, group_name="CS24"))
        await s.commit()

    rules = (await client.get("/api/alerts/server-rules", headers=auth_headers)).json()["rules"]
    for v in rules.values():
        v["scope_type"], v["scope"] = "groups", ["VDNH"]
    off = sorted(rules)[0]           # одно правило выключаем — его трогать нельзя
    rules[off]["enabled"] = False
    await client.put("/api/alerts/server-rules", headers=auth_headers, json={"rules": rules})

    items = (await client.get("/api/alerts/coverage", headers=auth_headers)).json()["items"]
    mute = [i for i in items if i["name"] == "чужая-группа"]
    assert mute and mute[0]["fixable"], "объект с группой чинится одной кнопкой"

    r = await client.post("/api/alerts/coverage/apply", headers=auth_headers,
                          json={"items": [{"kind": "server", "id": mute[0]["id"]}]})
    assert r.status_code == 200, r.text
    assert r.json()["rules"] > 0, "ни одно правило не расширено"
    assert not any(i["name"] == "чужая-группа" for i in r.json()["items"]), "объект всё ещё немой"

    after = (await client.get("/api/alerts/server-rules", headers=auth_headers)).json()["rules"]
    assert "CS24" in after[[k for k in after if k != off][0]]["scope"]
    assert after[off]["scope"] == ["VDNH"], "выключенное правило тронули"
    assert after[off]["enabled"] is False, "выключенное правило включили"
