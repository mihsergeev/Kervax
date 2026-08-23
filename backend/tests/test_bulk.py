import httpx


async def test_bulk_update_applies_to_all(client: httpx.AsyncClient, auth_headers):
    for i in range(2):
        r = await client.post(
            "/api/checks",
            json={"name": f"m{i}", "type": "http", "target": "https://x"},
            headers=auth_headers,
        )
        assert r.status_code == 201

    r = await client.patch(
        "/api/checks/bulk",
        json={"retries": 3, "alert_after_failures": 3, "ssl_warn_days": [1, 7, 14]},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json()["updated"] == 2

    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    for c in ov["checks"]:
        assert c["retries"] == 3 and c["alert_after_failures"] == 3
        assert c["ssl_warn_days"] == [14, 7, 1]  # нормализовано (по убыванию)


async def test_bulk_update_empty_noop(client: httpx.AsyncClient, auth_headers):
    r = await client.patch("/api/checks/bulk", json={}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["updated"] == 0


async def test_import_checks(client: httpx.AsyncClient, auth_headers):
    r = await client.post(
        "/api/checks/import",
        json={"items": [
            {"name": "a", "type": "http", "target": "https://a.com", "group_name": "G"},
            {"name": "b", "type": "http", "target": "https://b.com", "group_name": "G"},
        ]},
        headers=auth_headers,
    )
    assert r.status_code == 201 and r.json()["updated"] == 2
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["total"] == 2 and all(c["group_name"] == "G" for c in ov["checks"])
