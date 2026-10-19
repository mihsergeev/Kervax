import httpx


async def test_retention_get_defaults_and_update(
    client: httpx.AsyncClient, auth_headers
):
    # дефолты берутся из env-конфига
    r = await client.get("/api/settings/retention", headers=auth_headers)
    assert r.status_code == 200
    # 30 дней снимков по умолчанию: 60 почти никогда не нужны, а таблица
    # снимков растёт быстрее всех (инциденты хранятся отдельно и не страдают)
    assert r.json() == {"server_days": 30, "sample_days": 30}

    # обновление сохраняется
    r = await client.put(
        "/api/settings/retention",
        json={"server_days": 180, "sample_days": 90},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json()["server_days"] == 180

    r = await client.get("/api/settings/retention", headers=auth_headers)
    assert r.json() == {"server_days": 180, "sample_days": 90}

    # выход за границы отвергается
    r = await client.put(
        "/api/settings/retention",
        json={"server_days": 0, "sample_days": 90},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_agent_report_captures_ips(client: httpx.AsyncClient, auth_headers):
    r = await client.post("/api/servers", json={"name": "ipsrv"}, headers=auth_headers)
    token = r.json()["token"]
    report = {
        "hostname": "h", "os": "Ubuntu", "agent_version": "1.4",
        "local_ip": "10.0.0.5", "cpu_percent": 1.0,
        "mem_used": 1, "mem_total": 2, "disks": [],
    }
    r = await client.post(
        "/api/agent/report",
        json=report,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Forwarded-For": "203.0.113.7, 172.18.0.1",  # первый — реальный клиент
        },
    )
    assert r.status_code == 200

    s = (await client.get("/api/servers", headers=auth_headers)).json()[0]
    assert s["local_ip"] == "10.0.0.5"
    assert s["external_ip"] == "203.0.113.7"


async def test_retention_overview(client: httpx.AsyncClient, auth_headers):
    """Окно "Хранение данных": размер базы, группы со сроком из настроек и постоянные сроки."""
    r = await client.post("/api/servers", json={"name": "web-a"}, headers=auth_headers)
    token = r.json()["token"]
    report = {"hostname": "h", "os": "Ubuntu", "agent_version": "1.4", "cpu_percent": 1.0,
              "mem_used": 1, "mem_total": 2, "disks": []}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    r = await client.get("/api/settings/retention/overview", headers=auth_headers)
    assert r.status_code == 200
    d = r.json()
    assert d["db_bytes"] > 0 and d["prune_every"] == 3600 and d["other_bytes"] >= 0
    srv, chk = d["groups"]
    assert (srv["id"], srv["days"], chk["id"], chk["days"]) == ("srv", 30, "chk", 30)
    assert srv["parts"][0]["key"] == "server_metrics" and srv["parts"][0]["rows"] == 1
    assert srv["oldest"] and srv["bytes"] > 0 and srv["per_day"] == srv["bytes"]  # накоплено меньше суток
    assert chk["oldest"] is None and chk["per_day"] == 0
    assert srv["span_days"] < 1  # прогноз в окне считает от того, что реально накоплено
    assert [(f["key"], f["days"]) for f in d["fixed"]] == [
        ("alerts", 180), ("timeline", 90), ("traffic", 400), ("usage", 8), ("commands", 7), ("web_lines", 7),
        ("domain_probes", 7), ("probes", 1), ("panel_requests", 2), ("panel_errors", 30)]
    assert next(f for f in d["fixed"] if f["key"] == "web_lines")["bytes"] is None  # колонка, не таблица
    # срок из настроек виден и здесь
    await client.put("/api/settings/retention", json={"server_days": 90, "sample_days": 14}, headers=auth_headers)
    d = (await client.get("/api/settings/retention/overview", headers=auth_headers)).json()
    assert [g["days"] for g in d["groups"]] == [90, 14]
