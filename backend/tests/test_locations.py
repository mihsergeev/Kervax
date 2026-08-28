from types import SimpleNamespace

import httpx
from sqlalchemy import func, select

from app import bootstrap, checks as checks_mod, collector
from app.checks import CheckOutcome
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, Location, LocationResult, LocationSample


async def test_location_crud(client: httpx.AsyncClient, auth_headers):
    r = await client.post(
        "/api/locations",
        json={"name": "Германия", "url": "http://proxy.de:3128"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    lid = r.json()["id"]
    assert r.json()["enabled"] is True

    r = await client.get("/api/locations", headers=auth_headers)
    assert r.status_code == 200 and len(r.json()) == 1

    r = await client.patch(
        f"/api/locations/{lid}", json={"enabled": False}, headers=auth_headers
    )
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = await client.delete(f"/api/locations/{lid}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get("/api/locations", headers=auth_headers)
    assert r.json() == []


async def test_location_probes(tmp_path, monkeypatch):
    db = (tmp_path / "loc.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="site", type="http", target="https://x",
                    enabled=True, check_locations=True))
        s.add(Check(name="nolocs", type="http", target="https://y",
                    enabled=True, check_locations=False))
        s.add(Location(name="DE", url="http://proxy.de:3128", enabled=True))
        s.add(Location(name="off", url="http://proxy.off:3128", enabled=False))
        await s.commit()

    seen: list[str] = []

    async def fake_probe(check, proxy_url):
        seen.append(proxy_url)
        return CheckOutcome("up", latency_ms=42, message="ok")

    monkeypatch.setattr("app.checks.probe_via_proxy", fake_probe)
    settings = Settings(location_probe_interval=300)

    # только (site × DE): монитор без check_locations и выключенная локация — мимо
    n = await collector.run_location_probes(factory, settings)
    assert n == 1 and seen == ["http://proxy.de:3128"]
    async with factory() as s:
        rows = list(await s.scalars(select(LocationResult)))
        assert len(rows) == 1 and rows[0].status == "up" and rows[0].latency_ms == 42
        samples = list(await s.scalars(select(LocationSample)))
        assert len(samples) == 1 and samples[0].latency_ms == 42  # + точка в тайм-серию

    # повторный прогон сразу — результат свежий, не перепроверяем
    n2 = await collector.run_location_probes(factory, settings)
    assert n2 == 0
    await engine.dispose()


async def test_location_probe_down_on_new_row_no_crash(tmp_path, monkeypatch):
    """Регресс: первая проба НОВОЙ пары (монитор, локация) с down не должна ронять
    всю пачку (у свежей LocationResult consecutive_fails ещё None → None+1). Раньше
    добавление первой прокси-локации ломало запись ВСЕХ локаций."""
    db = (tmp_path / "locdown.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="bad", type="http", target="https://x",
                    enabled=True, check_locations=True))
        s.add(Check(name="good", type="http", target="https://y",
                    enabled=True, check_locations=True))
        s.add(Location(name="KZ", url="http://proxy.kz:3128", enabled=True))
        await s.commit()

    async def fake_probe(check, proxy_url):
        # «bad» падает, «good» — ок; обе строки — новые (consecutive_fails None)
        if check.name == "bad":
            return CheckOutcome("down", message="timeout")
        return CheckOutcome("up", latency_ms=50, message="ok")

    monkeypatch.setattr("app.checks.probe_via_proxy", fake_probe)
    n = await collector.run_location_probes(factory, Settings(location_probe_interval=300))
    assert n == 2  # обе пробы прошли, пачка не упала
    async with factory() as s:
        rows = {
            (await s.get(Check, r.check_id)).name: r
            for r in await s.scalars(select(LocationResult))
        }
        assert rows["bad"].status == "down" and rows["bad"].consecutive_fails == 1
        assert rows["good"].status == "up" and rows["good"].consecutive_fails == 0
    await engine.dispose()


def test_effective_locations():
    enabled = [SimpleNamespace(id=2), SimpleNamespace(id=3), SimpleNamespace(id=4)]
    eff = collector.effective_locations
    assert [x.id for x in eff(SimpleNamespace(location_ids=None), enabled)] == [2, 3, 4]
    assert eff(SimpleNamespace(location_ids=[]), enabled) == []  # ни одной
    # подмножество в порядке enabled, несуществующие id игнорируются
    assert [x.id for x in eff(SimpleNamespace(location_ids=[4, 2, 99]), enabled)] == [2, 4]


async def test_probe_direct_uses_no_proxy(monkeypatch):
    seen: list = []

    async def fake_http(check, proxy=None, degraded_ms=None, timeout_ms=None):
        seen.append(proxy)
        return CheckOutcome("up")

    monkeypatch.setattr("app.checks._run_http", fake_http)
    check = SimpleNamespace(degraded_ms=2000, timeout_ms=10000)
    await checks_mod.probe_via_proxy(check, "")  # пусто → напрямую
    await checks_mod.probe_via_proxy(check, "http://p:3128")
    assert seen == [None, "http://p:3128"]


async def test_location_partial_alert(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db = (tmp_path / "la.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="site", type="http", target="https://x",
                    enabled=True, check_locations=True, last_status="up"))
        s.add(Location(name="Прямая", url="", enabled=True))
        s.add(Location(name="Прокси", url="http://p:3128", enabled=True))
        await s.commit()
    async with factory() as s:
        proxy = (await s.scalars(select(Location).where(Location.url != ""))).one()
        cid = await s.scalar(select(Check.id))
        # 1 сбой подряд — транзиент (порог alert_after_failures=3): пока не алертим
        s.add(LocationResult(check_id=cid, location_id=proxy.id,
                             status="down", latency_ms=None, message="fail",
                             consecutive_fails=1))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    now = datetime.now(timezone.utc)
    settings = Settings(alert_webhook="http://hook", panel_url="https://p.example.com/")

    # дебаунс: прокси упала лишь 1 раз → не считаем упавшей, алерта нет
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []

    # 3 подряд-сбоя (≥ порога) → устойчивое падение. Но сразу не алертим: точки
    # ходят каждая на своей каденции, и «видно отсюда, не видно оттуда» надо
    # сперва подтвердить временем (см. _LOC_PARTIAL_SUSTAIN).
    async with factory() as s:
        pr = (await s.scalars(select(LocationResult))).one()
        pr.consecutive_fails = 3
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []

    # состояние держится дольше выдержки → частичная доступность
    now = now + timedelta(seconds=collector._LOC_PARTIAL_SUSTAIN + 30)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert len(sent) == 1
    assert "🟢 Прямая — доступен" in sent[0] and "🔴 Прокси — недоступен" in sent[0]
    assert "https://p.example.com/?check=" in sent[0]  # диплинк на монитор

    # тот же набор «упавших» → без повторного алерта
    sent.clear()
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []

    # прокси вернулся → recovery «снова доступен отовсюду»
    async with factory() as s:
        r = (await s.scalars(select(LocationResult))).one()
        r.status = "up"
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    assert len(sent) == 1 and "снова доступен" in sent[0]
    await engine.dispose()


async def test_default_location_seeded_once(tmp_path, monkeypatch):
    db = (tmp_path / "seed.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def fake_geo():
        return "Финляндия, Хельсинки"

    monkeypatch.setattr("app.bootstrap._panel_geo_name", fake_geo)
    await bootstrap.ensure_default_location(factory, Settings())
    async with factory() as s:
        locs = list(await s.scalars(select(Location)))
        assert len(locs) == 1
        assert locs[0].url == "" and locs[0].name == "Финляндия, Хельсинки"

    # идемпотентно: второй вызов не плодит локации
    await bootstrap.ensure_default_location(factory, Settings())
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Location)) == 1
    await engine.dispose()


async def test_proxy_test_endpoint(client: httpx.AsyncClient, auth_headers, monkeypatch):
    """POST /locations/test: пустой url = ок (напрямую); рабочий прокси = ок;
    недоступный = ok False с текстом ошибки."""
    # напрямую — всегда доступно
    r = await client.post("/api/locations/test", json={"url": ""}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["ok"] is True

    # мокаем httpx: любой прокси «отвечает» 204
    class FakeResp:
        status_code = 204

    class OkClient:
        def __init__(self, **kw):
            assert kw.get("proxy")  # прокси реально передаётся

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr("app.checks.httpx.AsyncClient", OkClient)
    r = await client.post(
        "/api/locations/test", json={"url": "socks5://203.0.113.10:1080"}, headers=auth_headers
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    # прокси недоступен → на всех целях исключение → ok False
    class DeadClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr("app.checks.httpx.AsyncClient", DeadClient)
    r = await client.post(
        "/api/locations/test", json={"url": "socks5://203.0.113.10:1080"}, headers=auth_headers
    )
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "refused" in r.json()["message"].lower()


async def test_probe_via_proxy_retries_and_timeout(monkeypatch):
    """Через прокси проверка смягчена: больший таймаут + повторы. Флап (down, потом
    up) → возвращаем up (не мигаем ложным down); стойкий down → down после повторов."""
    from types import SimpleNamespace

    from app import checks as ce
    from app.checks import CheckOutcome
    from app.config import Settings

    monkeypatch.setattr("app.checks.get_settings",
                        lambda: Settings(location_retries=2, location_timeout_extra_ms=10000,
                                         location_degraded_extra_ms=2000, check_retry_delay_ms=0))
    seq = ["down", "up"]  # первая проба упала, вторая — успех
    seen_timeouts: list = []

    async def fake_http(check, proxy=None, degraded_ms=None, timeout_ms=None):
        seen_timeouts.append(timeout_ms)
        return CheckOutcome(seq.pop(0))

    monkeypatch.setattr("app.checks._run_http", fake_http)
    check = SimpleNamespace(degraded_ms=2000, timeout_ms=10000)
    out = await ce.probe_via_proxy(check, "http://p:3128")
    assert out.status == "up"  # ретрай спас флап
    assert seen_timeouts[0] == 20000  # таймаут = check.timeout_ms + extra (10000+10000)

    # стойкий down → всё равно down (после 1+2 попыток)
    seq2 = ["down", "down", "down"]
    async def fake_down(check, proxy=None, degraded_ms=None, timeout_ms=None):
        return CheckOutcome(seq2.pop(0))
    monkeypatch.setattr("app.checks._run_http", fake_down)
    out = await ce.probe_via_proxy(check, "http://p:3128")
    assert out.status == "down" and not seq2  # все 3 попытки использованы


async def test_partial_state_saved_without_alert_channel(tmp_path, monkeypatch):
    """«Частично» должно быть видно в панели даже когда алерты не настроены.

    loc_alerted — это не только дедупликация алерта: на нём держатся счётчик
    «частично», чип с именем точки и сводка проблемных точек. Раньше он
    применялся только после УСПЕШНОЙ доставки, поэтому на установке без
    Telegram и webhook — то есть на любой свежей — панель молчала о том, что
    сайт не виден из части точек, хотя все данные у неё были.
    """
    from datetime import datetime, timezone

    db = (tmp_path / "ls.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="site", type="http", target="https://x", enabled=True,
                    check_locations=True, last_status="up"))
        s.add(Location(name="Прямая", url="", enabled=True))
        s.add(Location(name="Прокси", url="http://p:3128", enabled=True))
        await s.commit()
    async with factory() as s:
        proxy = (await s.scalars(select(Location).where(Location.url != ""))).one()
        cid = await s.scalar(select(Check.id))
        s.add(LocationResult(check_id=cid, location_id=proxy.id, status="down",
                             latency_ms=None, message="fail", consecutive_fails=3))
        await s.commit()
        proxy_id = proxy.id

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return ["канал не настроен"]        # доставки нет

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    # ни webhook, ни telegram — как на свежей установке
    await collector.evaluate_location_alerts(
        factory, Settings(), datetime.now(timezone.utc)
    )

    async with factory() as s:
        row = await s.scalar(select(Check))
        assert row.loc_alerted == [proxy_id], (
            f"панель не запомнила проблемную точку: loc_alerted={row.loc_alerted}"
        )
    await engine.dispose()


async def test_recovery_from_full_outage_is_silent(tmp_path, monkeypatch):
    """Подъём после полного падения не должен слать «недоступен из N».

    Поймано вживую: на сервере меняли диск, сайт лёг целиком, а при возврате
    точки проверки поднялись не одновременно — сперва одна, через минуту вторая.
    На каждый монитор прилетала лишняя пара «Алматы недоступен» + «снова доступен
    из всех локаций», хотя чинить было нечего: сайт просто вставал.
    """
    from datetime import datetime, timedelta, timezone

    db = (tmp_path / "lr.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="cake.ru", type="http", target="https://cake.ru",
                    enabled=True, check_locations=True, last_status="down",
                    consecutive_fails=5))
        s.add(Location(name="Россия, Санкт-Петербург", url="", enabled=True))
        s.add(Location(name="Казахстан, Алматы", url="http://kz:3128", enabled=True))
        await s.commit()
    async with factory() as s:
        proxy = (await s.scalars(select(Location).where(Location.url != ""))).one()
        cid = await s.scalar(select(Check.id))
        s.add(LocationResult(check_id=cid, location_id=proxy.id, status="down",
                             latency_ms=None, message="fail", consecutive_fails=5))
        await s.commit()
        proxy_id = proxy.id

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    now = datetime.now(timezone.utc)

    # сайт лежит целиком: частичной доступности нет, отсчёт не идёт
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []
    async with factory() as s:
        assert (await s.scalar(select(Check.loc_partial_since))) is None

    # лежит уже десять минут — дольше любой выдержки
    now = now + timedelta(minutes=10)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []

    # диск заменили, сайт встаёт: прямая проверка поднялась, прокси ещё нет
    async with factory() as s:
        row = (await s.scalars(select(Check))).one()
        row.last_status, row.consecutive_fails = "up", 0
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == [], "фаза восстановления не должна выглядеть как частичная недоступность"
    # интерфейсу правда видна сразу, а отсчёт начат с нуля — не с начала падения
    async with factory() as s:
        row = (await s.scalars(select(Check))).one()
        assert row.loc_alerted == [proxy_id]
        # sqlite отдаёт время без зоны — сравниваем по значению
        assert row.loc_partial_since.replace(tzinfo=timezone.utc) == now

    # через минуту поднялась и вторая точка
    now = now + timedelta(minutes=1)
    async with factory() as s:
        r = (await s.scalars(select(LocationResult))).one()
        r.status, r.consecutive_fails = "up", 0
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    # ни «недоступен из N», ни отбоя по алерту, которого не было
    assert sent == []
    async with factory() as s:
        row = (await s.scalars(select(Check))).one()
        assert row.loc_alerted is None and row.loc_partial_since is None
    await engine.dispose()


async def test_partial_outage_that_lasts_still_alerts(tmp_path, monkeypatch):
    """Выдержка гасит фазу восстановления, но не саму находку.

    Если сайт действительно не виден из региона — алерт обязан прийти, просто
    позже. Прошлый инцидент был именно такой: 20 минут не видно из одной точки.
    """
    from datetime import datetime, timedelta, timezone

    db = (tmp_path / "lp.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="site", type="http", target="https://x", enabled=True,
                    check_locations=True, last_status="up"))
        s.add(Location(name="Прямая", url="", enabled=True))
        s.add(Location(name="Алматы", url="http://kz:3128", enabled=True))
        await s.commit()
    async with factory() as s:
        proxy = (await s.scalars(select(Location).where(Location.url != ""))).one()
        cid = await s.scalar(select(Check.id))
        s.add(LocationResult(check_id=cid, location_id=proxy.id, status="down",
                             latency_ms=None, message="fail", consecutive_fails=3))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    now = datetime.now(timezone.utc)

    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []                      # ждём подтверждения временем

    now = now + timedelta(minutes=20)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert len(sent) == 1 and "Алматы — недоступен" in sent[0]

    # и отбой по нему приходит, потому что алерт был
    async with factory() as s:
        r = (await s.scalars(select(LocationResult))).one()
        r.status, r.consecutive_fails = "up", 0
        await s.commit()
    sent.clear()
    await collector.evaluate_location_alerts(factory, settings, now + timedelta(minutes=1))
    assert len(sent) == 1 and "снова доступен" in sent[0]
    await engine.dispose()
