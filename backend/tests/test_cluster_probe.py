"""Сайт в Kubernetes за белым списком шлюза — проверка изнутри в обход шлюза.

Живой случай (17.09.2026, ru-cs24-foundry, k0s + Envoy Gateway): стенд опубликован через
шлюз с SecurityPolicy-вайтлистом. Снаружи панель получает 403, на localhost шлюз не слушает
(«порт закрыт»), а саму ноду вайтлист тоже не пускает. Агент 2.9 в таком случае спрашивает
сервис маршрута напрямую, сертификат берёт у шлюза. Панель обязана говорить, каким путём
проверено: зелёный статус здесь значит «приложение отвечает», а не «сайт открылся через шлюз».
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from app import checks as checks_exec
from app.models import AgentProbe, Check

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CHECK = SimpleNamespace(interval_seconds=60, expected_status="200-399", keyword_up="",
                        keyword_down="", probe_server_id=1, target="https://foundry-dev.corplab.ai/health")


def _probe(**kw):
    base = dict(ts=NOW, code=0, latency_ms=0, error="", kw_up_found=True, kw_down_found=False,
                via="cluster:default/api-gateway:8080")
    return SimpleNamespace(**(base | kw))


def test_app_answers_through_its_service():
    out = checks_exec.outcome_from_agent(CHECK, _probe(code=200, latency_ms=2), NOW, 2000)
    assert out.status == "up"
    assert out.message == "HTTP 200 · 2 мс · сервис default/api-gateway:8080 в обход шлюза"


def test_gateway_down_is_down_even_if_the_app_answers():
    err = "gateway 192.168.0.37:443: dial tcp 192.168.0.37:443: connect: connection refused"
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err), NOW, 2000)
    assert out.status == "down" and out.message.startswith("шлюз 192.168.0.37:443 не отвечает")


def test_service_error_names_the_service_not_localhost():
    err = 'Get "http://10.98.0.27:8080/health": dial tcp 10.98.0.27:8080: connect: connection refused'
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err), NOW, 2000)
    assert out.status == "down"
    assert "в обход шлюза" in out.message and "localhost" not in out.message
    # код приложения оценивается как обычно
    assert checks_exec.outcome_from_agent(CHECK, _probe(code=503, latency_ms=5), NOW, 2000).status == "down"


def test_localhost_result_keeps_its_old_wording():
    err = 'Get "https://foundry-dev.corplab.ai": dial tcp 127.0.0.1:443: connect: connection refused'
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err, via=""), NOW, 2000)
    assert "localhost" in out.message


REPORT = {
    "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.9",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
}


async def test_report_stores_how_the_site_was_probed(client: httpx.AsyncClient, auth_headers):
    r = await client.post("/api/servers", json={"name": "ru-cs24-foundry"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {r.json()['token']}"}
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as s:
        chk = Check(name="foundry", type="http", target="https://foundry-dev.corplab.ai/health",
                    probe_local=True, probe_server_id=sid)
        s.add(chk)
        await s.commit()
        cid = chk.id
    r = await client.post("/api/agent/report", headers=ah, json=dict(REPORT, site_probes=[
        {"id": cid, "code": 200, "latency_ms": 2, "kw_up_found": True,
         "cert_expires": 1795522402, "cert_issuer": "YR1", "via": "cluster:default/api-gateway:8080"},
    ]))
    assert r.status_code == 200, r.text
    async with factory() as s:
        row = (await s.scalars(select(AgentProbe).where(AgentProbe.check_id == cid))).one()
        assert row.via == "cluster:default/api-gateway:8080" and row.cert_issuer == "YR1"
