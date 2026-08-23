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
