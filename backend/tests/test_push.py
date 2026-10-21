"""Push-мониторы: скрипт сам стучится в панель, нет сигнала вовремя - монитор падает."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import checks
from app.models import Check


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _create(client, auth_headers, **kw):
    body = {"name": "ночной бэкап", "type": "push", "target": "push", "push_period": 3600, **kw}
    r = await client.post("/api/checks", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_push_monitor_gets_token_and_defaults(client, auth_headers):
    j = await _create(client, auth_headers, retries=3, check_locations=True, interval_seconds=600)
    assert len(j["push_token"]) >= 24 and j["target"] == "push"
    assert j["interval_seconds"] == 60 and j["retries"] == 0 and j["alert_after_failures"] == 1
    assert j["check_locations"] is False and j["push_period"] == 3600


async def test_push_endpoint_marks_signal_and_failure(client, auth_headers):
    j = await _create(client, auth_headers)
    tok = j["push_token"]
    r = await client.get(f"/api/agent/push/{tok}")
    assert r.status_code == 200 and r.json() == {"ok": True}
    async with _factory(client)() as s:
        c = await s.get(Check, j["id"])
        assert c.push_at is not None and c.push_fail == ""
    r = await client.post(f"/api/agent/push/{tok}?status=1&msg=rsync%20упал")
    assert r.status_code == 200
    async with _factory(client)() as s:
        assert (await s.get(Check, j["id"])).push_fail == "rsync упал"
    await client.get(f"/api/agent/push/{tok}?status=0")
    async with _factory(client)() as s:
        assert (await s.get(Check, j["id"])).push_fail == ""


async def test_push_endpoint_unknown_token(client):
    assert (await client.get("/api/agent/push/" + "x" * 32)).status_code == 404
    assert (await client.get("/api/agent/push/short")).status_code == 404


async def test_rotate_token_invalidates_old(client, auth_headers):
    j = await _create(client, auth_headers)
    r = await client.post(f"/api/checks/{j['id']}/push-token", headers=auth_headers)
    assert r.status_code == 200 and r.json()["push_token"] != j["push_token"]
    assert (await client.get(f"/api/agent/push/{j['push_token']}")).status_code == 404
    assert (await client.get(f"/api/agent/push/{r.json()['push_token']}")).status_code == 200


def _chk(**kw):
    base = dict(type="push", push_period=3600, push_grace=0, push_at=None, push_fail="",
                created_at=datetime.now(timezone.utc), retries=0)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_runner_statuses():
    now = datetime.now(timezone.utc)
    # еще не стучался, но период не вышел - ждем
    out = await checks.run_check(_chk())
    assert out.status == "up" and out.message == "ждем первый сигнал"
    # не стучался ни разу за период и допуск
    out = await checks.run_check(_chk(created_at=now - timedelta(hours=2)))
    assert out.status == "down" and "ни разу" in out.message
    # стучался 30 минут назад - ок
    out = await checks.run_check(_chk(push_at=now - timedelta(minutes=30)))
    assert out.status == "up" and out.message == "сигнал 30 мин назад"
    # час + допуск 6 минут (десятая часть часа) уже прошли
    out = await checks.run_check(_chk(push_at=now - timedelta(minutes=67)))
    assert out.status == "down" and out.message == "нет сигнала 1 ч 7 мин, ждали каждые 1 ч"
    # в допуске - еще ок
    assert (await checks.run_check(_chk(push_at=now - timedelta(minutes=65)))).status == "up"
    # стучался вовремя, но сообщил об ошибке
    out = await checks.run_check(_chk(push_at=now - timedelta(minutes=1), push_fail="rsync упал"))
    assert out.status == "down" and out.message == "скрипт сообщил об ошибке: rsync упал"


def test_grace_defaults():
    assert checks.push_limits(_chk(push_period=60)) == (60, 120)          # не меньше 2 минут
    assert checks.push_limits(_chk(push_period=86400)) == (86400, 8640)   # десятая часть
    assert checks.push_limits(_chk(push_period=3600, push_grace=900)) == (3600, 900)


async def test_push_alert_uses_name_and_does_not_fold(client, monkeypatch):
    """У push-монитора нет адреса: в алерте его имя, а два упавших push-монитора - два
    сообщения, а не одно "и еще" (свертка по домену для них не имеет смысла)."""
    from app import alerts, collector
    from app.config import Settings

    f = _factory(client)
    async with f() as s:
        a = Check(name="бэкап-a", type="push", target="push", push_token="a" * 24)
        b = Check(name="бэкап-b", type="push", target="push", push_token="b" * 24)
        s.add_all([a, b])
        await s.commit()
        ia, ib = a.id, b.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    pending = [("bad", "бэкап-a", "down", "нет сигнала 2 ч", None, ia, None),
               ("bad", "бэкап-b", "down", "нет сигнала 2 ч", None, ib, None)]
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"), pending,
                                 datetime.now(timezone.utc))
    assert len(sent) == 2
    # в чат: имя монитора в шапке, что случилось и причина; адреса push-монитора нет нигде
    assert str(sent[0]).split("\n")[0].endswith(" бэкап-a") and "<b>Не отвечает</b>" in sent[0]
    assert "нет сигнала 2 ч" in sent[0] and "push" not in str(sent[0])
    full = alerts.full_text(sent[0])
    assert "бэкап-a" in full and "нет сигнала 2 ч" in full and "push" not in full.split("—")[0]
    assert "бэкап-b" in sent[1] and "бэкап-a" not in sent[1]


async def test_viewer_does_not_get_token(client, auth_headers):
    """Адрес push-монитора - секрет: кто его знает, тот гасит алерт за упавший крон."""
    j = await _create(client, auth_headers)
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "watcher", "password": "watcher-pass-2026", "role": "viewer",
    })
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login",
                              json={"username": "watcher", "password": "watcher-pass-2026"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get(f"/api/checks/{j['id']}", headers=vh)).json()["push_token"] == ""
    rows = (await client.get("/api/checks", headers=vh)).json()
    assert all(c["push_token"] == "" for c in rows)
    assert (await client.post(f"/api/checks/{j['id']}/push-token", headers=vh)).status_code == 403
    # маскировка в ответе не стерла токен в базе
    assert (await client.get(f"/api/agent/push/{j['push_token']}")).status_code == 200
