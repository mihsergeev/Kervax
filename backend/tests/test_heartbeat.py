async def test_heartbeat_write_webhook(tmp_path):
    from app import heartbeat, settings_store
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    db = (tmp_path / "hb.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        await settings_store.set_alert_config(s, "", "", "http://hook", "", 6)
        await s.commit()

    data = tmp_path / "data"
    settings = Settings(data_dir=str(data), panel_url="https://kervax.example")
    async with factory() as s:
        cfg = await settings_store.get_alert_config(s, settings)
    ok = await heartbeat.probe_alert_channel(cfg)  # только вебхук → True
    await heartbeat.write_heartbeat(factory, settings, cfg, ok)

    hb = (data / "heartbeat").read_text()
    assert "ts=" in hb
    assert "alerts_ok=1" in hb  # только вебхук → канал не долбим, считаем рабочим
    assert "webhook=http://hook" in hb
    assert "panel=https://kervax.example" in hb  # watchdog назовёт эту панель
    await engine.dispose()


async def test_heartbeat_loop_writes_immediately(tmp_path, monkeypatch):
    """heartbeat_loop пишет пульс СРАЗУ на старте (не ждёт первого тика/60с) —
    иначе рестарт создавал бы окно без пульса и ложное срабатывание watchdog."""
    import asyncio

    from app import heartbeat
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    db = (tmp_path / "hbl.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    data = tmp_path / "data"
    settings = Settings(data_dir=str(data))

    task = asyncio.create_task(heartbeat.heartbeat_loop(factory, settings))
    for _ in range(50):  # ждём первую запись (пишется до первого sleep)
        if (data / "heartbeat").exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert (data / "heartbeat").exists()
    await engine.dispose()


async def test_heartbeat_probe_channel(monkeypatch):
    from app import heartbeat

    # каналов нет → проверять нечего → ок
    assert await heartbeat.probe_alert_channel({}) is True

    cfg = {"telegram_token": "t", "telegram_chat": "c", "telegram_api": "", "webhook": ""}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return Resp()

    monkeypatch.setattr("app.heartbeat.httpx.AsyncClient", FakeClient)
    assert await heartbeat.probe_alert_channel(cfg) is True

    class FailClient(FakeClient):
        async def get(self, url):
            raise RuntimeError("api.telegram.org заблокирован")

    monkeypatch.setattr("app.heartbeat.httpx.AsyncClient", FailClient)
    assert await heartbeat.probe_alert_channel(cfg) is False


async def test_heartbeat_drops_unsafe_channel_values(tmp_path):
    """Пульс читает root-скрипт сторожа: старое значение в базе или из бэкапа мимо API не должно
    дописать в файл свои строки или подсунуть curl ключ вместо адреса."""
    from app import heartbeat
    from app.config import Settings

    data = tmp_path / "data"
    settings = Settings(data_dir=str(data), panel_url="https://kervax.example.test")
    cfg = {"telegram_token": "123456:TEST-token",
           "telegram_chat": "-1001234567890\nurl=file:///etc/passwd\noutput=/etc/cron.d/x",
           "telegram_api": "file:///etc/passwd", "webhook": f"-K{data / 'heartbeat'}"}
    await heartbeat.write_heartbeat(None, settings, cfg, False)
    lines = (data / "heartbeat").read_text().splitlines()
    assert not [x for x in lines if x.startswith(("url=", "output="))]
    assert {"tg_token=123456:TEST-token", "tg_chat=", "tg_api=", "webhook="} <= set(lines)

    cfg = {"telegram_token": "123456:TEST-token", "telegram_chat": "-1001234567890",
           "telegram_api": "http://192.0.2.10:8081", "webhook": "https://hook.example.test/kervax"}
    await heartbeat.write_heartbeat(None, settings, cfg, True)
    lines = (data / "heartbeat").read_text().splitlines()
    assert {"tg_chat=-1001234567890", "tg_api=http://192.0.2.10:8081",
            "webhook=https://hook.example.test/kervax"} <= set(lines)
