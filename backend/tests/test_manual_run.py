"""«Проверить сейчас»: ответ на нажатие, согласованный с инцидентами и агентом.

Живой случай (13.09.2026, corpsoft24): у сайта с локальной проверкой кнопка ходила
из панели, панель стояла в белом списке и получала «работает», а агент изнутри
продолжал видеть сбой. Монитор зеленел, в карточке висел инцидент «идёт сейчас», на
главной — красное «Проблемы: 0». Человек нажал кнопку двадцать раз и так и не понял,
работает сайт или нет.
"""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import checks as checks_exec
from app.models import AgentProbe, Check, CheckIncident, ProbeRequest, Server

REPORT = {
    "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.6",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
}


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


def _capture_alerts(monkeypatch) -> list:
    sent: list = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(str(text))
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return sent


async def _alerts_on(client, auth_headers) -> None:
    r = await client.put("/api/alerts", json={"webhook": "http://hook.example"},
                         headers=auth_headers)
    assert r.status_code == 200


async def _open_incident(client, cid: int, *, notified: bool) -> None:
    async with _factory(client)() as s:
        row = await s.get(Check, cid)
        row.last_status = "down"
        row.consecutive_fails = 5
        s.add(CheckIncident(
            check_id=cid, status="down", started_at=datetime.now(timezone.utc),
            last_message="сбой", notified=notified,
        ))
        await s.commit()


async def _incidents(client, cid: int) -> list[CheckIncident]:
    async with _factory(client)() as s:
        return list(await s.scalars(select(CheckIncident).where(CheckIncident.check_id == cid)))


async def test_manual_success_closes_incident_with_recovery(client, auth_headers, monkeypatch):
    """Сайт ответил на нажатие — инцидент закрыт, и отбой ушёл тем, кого будили."""
    await _alerts_on(client, auth_headers)
    sent = _capture_alerts(monkeypatch)

    async def up(check):
        return checks_exec.CheckOutcome("up", latency_ms=21, message="HTTP 200 · 21 мс")

    monkeypatch.setattr("app.checks.run_check", up)

    async def no_expiry(check):
        return checks_exec.ExpiryInfo()

    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    r = await client.post("/api/checks", json={"name": "s", "type": "http",
                                               "target": "https://s.example"},
                          headers=auth_headers)
    cid = r.json()["id"]
    await _open_incident(client, cid, notified=True)

    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_status"] == "up" and body["run_message"] == "HTTP 200 · 21 мс"
    assert body["run_source"] == "panel" and body["run_pending"] is None
    assert body["last_status"] == "up"

    incs = await _incidents(client, cid)
    assert len(incs) == 1 and incs[0].ended_at is not None, "инцидент остался открытым"
    assert any("✅" in m for m in sent), "отбой не ушёл"
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["open_incidents"] == 0


async def test_manual_failure_opens_incident_but_does_not_alert(client, auth_headers, monkeypatch):
    """Нажатие, попавшее на сбой, — честный красный статус и инцидент, но не алерт:
    порог «N неудачных подряд» задуман как минуты простоя, а не как три клика."""
    await _alerts_on(client, auth_headers)
    sent = _capture_alerts(monkeypatch)

    async def down(check):
        return checks_exec.CheckOutcome("down", message="HTTP 503")

    monkeypatch.setattr("app.checks.run_check", down)

    async def no_expiry(check):
        return checks_exec.ExpiryInfo()

    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    r = await client.post("/api/checks", json={"name": "s", "type": "http",
                                               "target": "https://s.example",
                                               "alert_after_failures": 1},
                          headers=auth_headers)
    cid = r.json()["id"]
    for _ in range(3):
        r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
        assert r.json()["run_status"] == "down"

    incs = await _incidents(client, cid)
    assert len(incs) == 1 and incs[0].ended_at is None and incs[0].notified is False
    async with _factory(client)() as s:
        assert (await s.get(Check, cid)).consecutive_fails == 0
    assert sent == []


async def _local_setup(client, auth_headers, monkeypatch, version="2.6"):
    """Сервер на связи + локальный монитор, привязанный к нему."""
    r = await client.post("/api/servers", json={"name": "node-a"}, headers=auth_headers)
    token = r.json()["token"]
    sid = r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/agent/report", json=dict(REPORT, agent_version=version),
                          headers=ah)
    assert r.status_code == 200

    async def panel_must_not_check(check):
        raise AssertionError("локальный сайт проверили из панели")

    monkeypatch.setattr("app.checks.run_check", panel_must_not_check)
    r = await client.post("/api/checks", json={"name": "closed", "type": "http",
                                               "target": "https://closed.example",
                                               "probe_local": True},
                          headers=auth_headers)
    cid = r.json()["id"]
    async with _factory(client)() as s:
        row = await s.get(Check, cid)
        row.probe_server_id = sid
        row.probe_bound_at = datetime.now(timezone.utc)
        await s.commit()
    return cid, sid, ah


async def test_local_manual_run_asks_the_agent(client, auth_headers, monkeypatch):
    await _alerts_on(client, auth_headers)
    sent = _capture_alerts(monkeypatch)
    cid, sid, ah = await _local_setup(client, auth_headers, monkeypatch)
    await _open_incident(client, cid, notified=True)
    # плановый ответ агента — старый сбой, который он будет повторять из памяти
    async with _factory(client)() as s:
        s.add(AgentProbe(check_id=cid, server_id=sid, ts=datetime.now(timezone.utc),
                         code=0, error='Get "https://closed.example": EOF'))
        await s.commit()

    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    rid = body["run_pending"]
    assert rid and body["run_source"] == "agent" and body["run_server"] == "node-a"
    assert body["run_fast"] is False
    # повторное нажатие, пока ждём, нового задания не плодит
    again = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert again.json()["run_pending"] == rid

    r = await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)
    assert r.json()["run_pending"] == rid and not r.json()["run_status"]

    # старый агент (2.6) быстрым опросом задание не получает — только отчётом
    r = await client.get("/api/agent/commands", headers=ah)
    assert r.json()["site_probes"] == []
    r = await client.post("/api/agent/report", json=REPORT, headers=ah)
    cfg = r.json()
    assert cfg["site_probes"][0]["id"] == -rid, "ручная проверка должна идти первой"
    assert {t["id"] for t in cfg["site_probes"]} == {-rid, cid}
    assert cfg["interval"] == 2, "пока ждём ответ, агент должен отчитываться чаще"

    # агент ответил: ручная проверка прошла, а плановый ответ — всё тот же старый сбой
    r = await client.post("/api/agent/report", headers=ah, json=dict(REPORT, site_probes=[
        {"id": -rid, "code": 200, "latency_ms": 7, "kw_up_found": True},
        {"id": cid, "code": 0, "latency_ms": 0, "error": 'Get "https://closed.example": EOF'},
    ]))
    cfg = r.json()
    assert cfg["interval"] == 15 and all(t["id"] > 0 for t in cfg["site_probes"])

    r = await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)
    body = r.json()
    assert body["run_pending"] is None and body["run_status"] == "up"
    assert body["run_message"] == "HTTP 200 · 7 мс" and body["run_latency_ms"] == 7
    assert body["last_status"] == "up"
    incs = await _incidents(client, cid)
    assert incs[0].ended_at is not None, "инцидент не закрыт ручной проверкой"
    assert any("✅" in m for m in sent)
    # старый плановый ответ не перебил свежий ручной — иначе через минуту снова красное
    async with _factory(client)() as s:
        probe = await s.get(AgentProbe, cid)
        assert probe.code == 200 and probe.manual_until is not None
        answered_at = probe.ts
        probe.ts = answered_at - timedelta(minutes=3)  # как будто ответ был давно
        await s.commit()
    # агент продолжает слать старый плановый сбой: данные не трогаем, но отметку
    # времени двигаем — иначе застывшая ts через пару минут читалась бы как молчание
    await client.post("/api/agent/report", headers=ah, json=dict(REPORT, site_probes=[
        {"id": cid, "code": 0, "latency_ms": 0, "error": 'Get "https://closed.example": EOF'},
    ]))
    async with _factory(client)() as s:
        probe = await s.get(AgentProbe, cid)
        assert probe.code == 200 and not probe.error
        assert probe.ts.replace(tzinfo=None) > (answered_at - timedelta(minutes=1)).replace(tzinfo=None)


async def test_local_manual_run_only_for_http(client, auth_headers, monkeypatch):
    """Изнутри агент умеет только HTTP: TCP-монитор с галочкой «локально» честно
    получает «так нельзя», а не непонятную ошибку разбора адреса."""
    cid, sid, ah = await _local_setup(client, auth_headers, monkeypatch)
    async with _factory(client)() as s:
        row = await s.get(Check, cid)
        row.type = "tcp_port"
        row.target = "closed.example"
        row.port = 5432
        await s.commit()
    body = (await client.post(f"/api/checks/{cid}/run", headers=auth_headers)).json()
    assert body["run_pending"] is None and "HTTP" in body["run_error"]
    async with _factory(client)() as s:
        assert (await s.scalars(select(ProbeRequest))).all() == []


async def test_local_manual_run_fast_agent(client, auth_headers, monkeypatch):
    """Агент 2.7+ забирает проверку опросом команд и отвечает сразу."""
    cid, sid, ah = await _local_setup(client, auth_headers, monkeypatch, version="2.7")
    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    rid = r.json()["run_pending"]
    assert r.json()["run_fast"] is True

    r = await client.get("/api/agent/commands", headers=ah)
    assert [t["id"] for t in r.json()["site_probes"]] == [-rid]
    # забранное быстрым путём второй раз не раздаём — ни опросом, ни отчётом
    r = await client.get("/api/agent/commands", headers=ah)
    assert r.json()["site_probes"] == []
    r = await client.post("/api/agent/report", json=dict(REPORT, agent_version="2.7"), headers=ah)
    assert all(t["id"] > 0 for t in r.json()["site_probes"]) and r.json()["interval"] == 15

    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": -rid, "code": 0, "latency_ms": 0,
         "error": 'Get "https://closed.example": dial tcp [::1]:443: connect: connection refused'},
    ]})
    assert r.status_code == 200, r.text
    body = (await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)).json()
    assert body["run_status"] == "down" and "localhost" in body["run_message"]
    assert body["run_latency_ms"] is None
    # чужой токен не может ответить за чужой запрос
    r = await client.post("/api/agent/site-probe-result",
                          headers={"Authorization": "Bearer wrong"},
                          json={"site_probes": [{"id": -rid, "code": 200}]})
    assert r.status_code == 401


async def test_local_manual_run_when_agent_cannot_answer(client, auth_headers, monkeypatch):
    cid, sid, ah = await _local_setup(client, auth_headers, monkeypatch)

    # запрос, на который агент так и не ответил, — «не удалось», статус не трогаем
    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    rid = r.json()["run_pending"]
    async with _factory(client)() as s:
        req = await s.get(ProbeRequest, rid)
        req.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
    body = (await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)).json()
    assert body["run_pending"] is None and "не прислал" in body["run_error"]
    assert body["last_status"] == "unknown"

    # нода не на связи — проверять изнутри некому, и это видно сразу
    async with _factory(client)() as s:
        srv = await s.get(Server, sid)
        srv.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
        await s.commit()
    body = (await client.post(f"/api/checks/{cid}/run", headers=auth_headers)).json()
    assert body["run_pending"] is None and "не на связи" in body["run_error"]


async def test_unchecking_local_drops_binding_and_agent_task(client, auth_headers, monkeypatch):
    """Сняли галочку «локально» — агент перестаёт проверять сайт. Раньше привязка
    оставалась, и агент ещё долго гонял проверку, которую никто не читал."""
    cid, sid, ah = await _local_setup(client, auth_headers, monkeypatch)
    r = await client.post("/api/agent/report", json=REPORT, headers=ah)
    assert [t["id"] for t in r.json()["site_probes"]] == [cid]

    r = await client.patch(f"/api/checks/{cid}", json={"probe_local": False},
                           headers=auth_headers)
    assert r.status_code == 200
    r = await client.post("/api/agent/report", json=REPORT, headers=ah)
    assert r.json()["site_probes"] == []
    async with _factory(client)() as s:
        assert (await s.get(Check, cid)).probe_server_id is None


async def _quiet_up(check):
    return checks_exec.CheckOutcome("up", latency_ms=5, message="HTTP 200 · 5 мс")


async def test_disabling_closes_incident(client, auth_headers, monkeypatch):
    """Выключенный монитор не проверяется — открытый инцидент закрыть было бы некому,
    и «1 откр. инцидентов» висело бы на главной вечно."""
    monkeypatch.setattr("app.checks.run_check", _quiet_up)  # первая проверка при создании
    r = await client.post("/api/checks", json={"name": "s", "type": "http",
                                               "target": "https://s.example"},
                          headers=auth_headers)
    cid = r.json()["id"]
    await _open_incident(client, cid, notified=False)
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["open_incidents"] == 1

    r = await client.patch(f"/api/checks/{cid}", json={"enabled": False}, headers=auth_headers)
    assert r.status_code == 200
    assert (await _incidents(client, cid))[0].ended_at is not None
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["open_incidents"] == 0


async def test_history_reports_bin_step(client, auth_headers, monkeypatch):
    """Карточка раскладывает ленту статуса на всё окно — ей нужна ширина бина."""
    monkeypatch.setattr("app.checks.run_check", _quiet_up)
    r = await client.post("/api/checks", json={"name": "s", "type": "http",
                                               "target": "https://s.example"},
                          headers=auth_headers)
    cid = r.json()["id"]
    r = await client.get(f"/api/checks/{cid}/history?hours=24", headers=auth_headers)
    assert r.json()["step_seconds"] == 288  # 24ч / 300 точек, интервал 60 с уже
    r = await client.get(f"/api/checks/{cid}/history?hours=1", headers=auth_headers)
    assert r.json()["step_seconds"] == 60  # не уже интервала проверок
