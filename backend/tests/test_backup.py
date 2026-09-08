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

def test_rotation_overflow_needs_time_and_zero_removals():
    """Мёртвую ротацию видно на вторые сутки: снапшотов больше политики, удалений нет.

    Возраст старейшего снапшота при keep-monthly 6 терпит 231 день — за это время диск
    активного бэкап-сервера кончится дважды. Но переполнение САМО ПО СЕБЕ законно:
    forget группирует по host+tags, и репозиторий с двумя клиентами держит два
    комплекта. Отличает мёртвую ротацию именно ноль удалений.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    bsrv = {"repos": [
        # ротация встала: снапшотов втрое больше политики и ничего не уходит
        {"name": "мертвый", "snapshots": 42, "keep_daily": 7, "keep_weekly": 4,
         "keep_monthly": 6, "rotation_removed": 0},
        # переполнен, но прогоны что-то удаляют — это просто несколько групп
        {"name": "многогруппный", "snapshots": 34, "keep_daily": 7, "keep_weekly": 4,
         "keep_monthly": 6, "rotation_removed": 5},
        # политики нет — судить не о чем
        {"name": "без-политики", "snapshots": 99, "rotation_removed": 0},
    ]}

    out, seen = collector.rotation_overflow(bsrv, {}, now)
    assert out == [], "сработал в первый же тик — единичный лок дал бы ложную тревогу"
    assert set(seen) == {"мертвый"}, seen

    old = {"мертвый": (now - timedelta(days=4)).isoformat()}
    out, seen = collector.rotation_overflow(bsrv, old, now)
    assert len(out) == 1 and out[0].startswith("мертвый")
    assert "42" in out[0] and "17" in out[0], "в тексте нет ни числа снапшотов, ни политики"

    # ротация ожила — отметка уходит, алерта нет
    bsrv["repos"][0]["rotation_removed"] = 11
    out, seen = collector.rotation_overflow(bsrv, old, now)
    assert out == [] and seen == {}
