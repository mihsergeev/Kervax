"""Где ломается сайт: фазы запроса монитора (DNS, подключение, TLS, ожидание ответа) и
проверка изнутри сервера сайта при первом сбое - ответом в том же алерте."""
import asyncio
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import checks as checks_exec
from app import collector
from app import manual_probe
from app.alerts import full_text
from app.config import Settings
from app.models import Check, CheckSample, ProbeRequest


async def _feed(ph, *events):
    for e in events:
        await ph._trace(e, {})


async def test_phases_from_trace_events():
    ph = checks_exec._Phases()
    ph.dns_ms = 3.2
    await _feed(ph, "connection.connect_tcp.started", "connection.connect_tcp.complete",
                "connection.start_tls.started", "connection.start_tls.failed", "http11.send_request_headers.started")
    r = ph.result(httpx.ConnectTimeout(""))
    assert r["fail"] == "tls" and r["dns"] == 3 and set(r) == {"dns", "connect", "tls", "fail"}
    assert ph.why(httpx.ConnectTimeout("")).startswith("соединение есть, но TLS-рукопожатие висит:")
    # не таймаут (сертификат, отказ) - текст ошибки остается как был
    assert ph.why(httpx.ConnectError("certificate verify failed")) == ""
    # удачный запрос: фазы без fail, через редирект складываются
    ok = checks_exec._Phases()
    await _feed(ok, "http11.receive_response_headers.started", "http11.receive_response_headers.complete",
                "http11.receive_response_headers.started", "http11.receive_response_headers.complete")
    assert set(ok.result()) == {"wait"}
    # адрес не нашелся: подключение упало на резолве - это DNS
    dns = checks_exec._Phases()
    dns.dns_ms, dns.dns_ok = 30.0, False
    await _feed(dns, "connection.connect_tcp.started", "connection.connect_tcp.failed")
    assert dns.result(httpx.ConnectError("x"))["fail"] == "dns"
    assert dns.why(httpx.ConnectTimeout("")) == "DNS не ответил за 0 с"
    assert checks_exec._secs(10400) == "10 с" and checks_exec._secs(2500) == "2.5 с" and checks_exec._secs(1000) == "1 с"


async def test_hang_points_at_the_phase():
    """Сервер принимает соединение и молчит: по http - висит ответ, по https - рукопожатие."""
    async def silent(reader, writer):
        try:
            await reader.read(65536)
            await asyncio.sleep(5)
        finally:
            writer.close()

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        chk = manual_probe.domain_check(f"http://127.0.0.1:{port}/")
        chk.timeout_ms = 1000
        out = await checks_exec._run_http(chk)
        assert out.status == "down" and out.message == "сервер принял запрос, но не ответил за 1 с", out.message
        assert out.phases["fail"] == "wait" and "connect" in out.phases and "dns" not in out.phases
        chk.target = f"https://127.0.0.1:{port}/"
        out = await checks_exec._run_http(chk)
        assert out.message == "соединение есть, но TLS-рукопожатие висит: 1 с без ответа", out.message
        assert out.phases["fail"] == "tls"
    finally:
        srv.close()
        await srv.wait_closed()


REPORT = {
    "hostname": "web-a", "os": "Ubuntu 24.04", "agent_version": "2.29",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
    "web_services": [{"kind": "nginx", "sites": ["shop.example"]}],
}


async def test_inside_probe_goes_into_the_alert(client, auth_headers, monkeypatch):
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    r = await client.post("/api/servers", json={"name": "web-a"}, headers=auth_headers)
    ah = {"Authorization": f"Bearer {r.json()['token']}"}
    assert (await client.post("/api/agent/report", json=REPORT, headers=ah)).status_code == 200
    async with factory() as s:
        row = Check(name="shop", type="http", target="https://shop.example", alert_after_failures=2,
                    check_ssl=False, check_domain=False)
        s.add(row)
        await s.commit()
        cid = row.id

    async def down(check):
        return checks_exec.CheckOutcome("down", message="сервер не принимает соединение: 10 с без ответа",
                                        phases={"dns": 4, "connect": 10000, "fail": "connect"})

    monkeypatch.setattr("app.checks.run_check", down)
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr("app.collector.get_settings", lambda: Settings(alert_webhook="http://hook"))
    cfg = Settings(alert_webhook="http://hook")

    # первый сбой: алерта нет (порог - два), но сервер сайта уже спросили
    assert await collector.run_due_checks(factory, cfg) == 1
    assert sent == []
    async with factory() as s:
        chk = await s.get(Check, cid)
        assert chk.last_phases == {"dns": 4, "connect": 10000, "fail": "connect"}
        assert chk.last_inside["server"] == "web-a" and "ts" not in chk.last_inside
        smp = (await s.scalars(select(CheckSample).where(CheckSample.check_id == cid))).one()
        assert (smp.ph_dns, smp.ph_connect, smp.ph_tls, smp.ph_wait) == (4, 10000, None, None)
        reqs = (await s.scalars(select(ProbeRequest))).all()
        assert [(q.check_id, q.kind) for q in reqs] == [(cid, "inside")]

    # агент забирает задание: только localhost, имя сайта из монитора
    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    assert len(tasks) == 1 and tasks[0]["id"] < 0 and tasks[0]["url"] == "https://shop.example"
    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": tasks[0]["id"], "code": 200, "latency_ms": 4, "kw_up_found": True}]})
    assert r.status_code == 200, r.text
    async with factory() as s:
        chk = await s.get(Check, cid)
        assert chk.last_inside["status"] == "up" and chk.last_inside["message"] == "HTTP 200 · 4 мс"
        assert chk.last_status == "down"  # ответ изнутри статус монитора не трогает
        chk.last_checked_at = None
        await s.commit()

    # второй сбой: алерт, в нем - что видно изнутри; новых заданий агенту нет
    assert await collector.run_due_checks(factory, cfg) == 1
    assert len(sent) == 1, sent
    # в чат - что случилось и причина; что видно изнутри сервера - в полном тексте (история, панель)
    assert sent[0].startswith("🔴🌐 ") and "<b>Не отвечает</b>" in sent[0]
    assert sent[0].endswith("\nсервер не принимает соединение: 10 с без ответа")
    full = full_text(sent[0])
    assert "изнутри web-a открывается (HTTP 200 · 4 мс): приложение живо" in full
    assert "сервер не принимает соединение: 10 с без ответа" in full
    async with factory() as s:
        assert len((await s.scalars(select(ProbeRequest))).all()) == 1

    # история: фазы в точках графика
    pts = (await client.get(f"/api/checks/{cid}/history?hours=1", headers=auth_headers)).json()["points"]
    assert any(p["ph_connect"] == 10000 and p["ph_dns"] == 4 for p in pts)
    out = (await client.get(f"/api/checks/{cid}", headers=auth_headers)).json()
    assert out["last_phases"]["fail"] == "connect" and out["last_inside"]["status"] == "up"


def test_alert_line_freshness():
    from app import inside_probe

    now = datetime(2026, 10, 14, 18, tzinfo=timezone.utc)
    li = {"ts": "2026-10-14T17:55:00+00:00", "server": "web-a", "status": "down",
          "message": "сервер отклонил соединение - порт закрыт"}
    assert inside_probe.alert_line(li, now) == "изнутри web-a тоже не открывается: сервер отклонил соединение - порт закрыт"
    assert inside_probe.alert_line(dict(li, ts="2026-10-14T17:40:00+00:00"), now) == ""  # старый ответ
    assert inside_probe.alert_line({"asked": "2026-10-14T17:59:00+00:00"}, now) == ""  # еще не ответил
    assert inside_probe.alert_line(None, now) == ""
