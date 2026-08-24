import httpx


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"]


async def test_health_reports_dead_database(client: httpx.AsyncClient, monkeypatch) -> None:
    """База недоступна — health обязан сказать об этом, а не отвечать «ok».

    Именно на этот ответ смотрят healthcheck контейнера и внешний мониторинг,
    поэтому «процесс жив» без «база отвечает» — ложное зелёное.
    """
    app = client._transport.app  # type: ignore[attr-defined]

    def broken_factory():
        raise OSError("connection refused")

    monkeypatch.setattr(app.state, "session_factory", broken_factory)
    response = await client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    assert body["version"]
