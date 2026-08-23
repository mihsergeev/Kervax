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
