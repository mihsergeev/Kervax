import httpx


async def test_backup_export_restore_roundtrip(
    client: httpx.AsyncClient, auth_headers
):
    # создаём монитор и сервер
    r = await client.post(
        "/api/checks",
        json={"name": "m1", "type": "http", "target": "https://ex.com"},
        headers=auth_headers,
    )
    assert r.status_code in (200, 201)
    r = await client.post("/api/servers", json={"name": "s1"}, headers=auth_headers)
    assert r.status_code == 201

    # экспорт: есть монитор, метрик НЕТ
    r = await client.get("/api/backup/export", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["app"] == "kervax"
    assert any(c["name"] == "m1" for c in data["tables"]["checks"])
    assert any(s["name"] == "s1" for s in data["tables"]["servers"])
    assert "server_metrics" not in data["tables"]
    assert "check_samples" not in data["tables"]

    # удаляем все мониторы
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    for c in ov["checks"]:
        await client.delete(f"/api/checks/{c['id']}", headers=auth_headers)
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["total"] == 0

    # восстановление возвращает монитор
    r = await client.post("/api/backup/restore", json=data, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["restored"]["checks"] >= 1

    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert any(c["name"] == "m1" for c in ov["checks"])


async def test_backup_restore_rejects_foreign_file(
    client: httpx.AsyncClient, auth_headers
):
    r = await client.post(
        "/api/backup/restore", json={"foo": "bar"}, headers=auth_headers
    )
    assert r.status_code == 400


async def test_backup_config_roundtrip(client: httpx.AsyncClient, auth_headers):
    r = await client.put(
        "/api/backup/config",
        json={"interval_hours": 12, "keep": 30},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json() == {"interval_hours": 12, "keep": 30}
    r = await client.get("/api/backup/config", headers=auth_headers)
    assert r.json()["interval_hours"] == 12
