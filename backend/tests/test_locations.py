from types import SimpleNamespace

import httpx
import pytest
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
    assert "sec=loc" in sent[0]  # сразу в раздел "Локации"

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
        s.add(Check(name="site.example", type="http", target="https://site.example",
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


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def test_locations_overview(client: httpx.AsyncClient, auth_headers):
    """Окно локаций: числа по каждой, сломанный прокси, порты нод и таблица "где недоступно"."""
    f = _factory(client)
    async with f() as s:
        direct = Location(name="Панель", url="", enabled=True)
        kz = Location(name="KZ", url="http://user:pass@203.0.113.10:3128", enabled=True)
        dead = Location(name="RU", url="socks5://203.0.113.30:1080", enabled=True)
        ports = Location(name="Порты", url="socks5://203.0.113.20:1080", enabled=True, monitors=False)
        s.add_all([direct, kz, dead, ports])
        a = Check(name="shop", type="http", target="https://shop.example", enabled=True,
                  check_locations=True, last_status="up")
        b = Check(name="api", type="http", target="https://api.example", enabled=True,
                  check_locations=True, last_status="up")
        c = Check(name="blog", type="http", target="https://blog.example", enabled=True,
                  check_locations=True, last_status="down", last_message="HTTP 502")
        s.add_all([a, b, c])
        await s.commit()
        for chk, kz_st in ((a, "down"), (b, "up"), (c, "down")):
            s.add(LocationResult(check_id=chk.id, location_id=kz.id, status=kz_st,
                                 message="connect timeout" if kz_st == "down" else "", consecutive_fails=3))
            s.add(LocationResult(check_id=chk.id, location_id=dead.id, status="down",
                                 message="прокси не отвечает", consecutive_fails=9))
            s.add(LocationSample(check_id=chk.id, location_id=kz.id, status=kz_st, latency_ms=1000))
        await s.commit()
        ids = {x.name: x.id for x in (direct, kz, dead, ports)}
        checks = {x.name: x.id for x in (a, b, c)}
        # планировщик подтвердил запросом через прокси, что RU не отвечает
        from app import settings_store
        await settings_store.set_raw(s, settings_store.LOC_BROKEN_KEY, '{"%d": {"notified": false}}' % dead.id)

    r = await client.get("/api/locations/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    by = {x["id"]: x for x in d["locations"]}
    # "напрямую" - основная проверка всех мониторов
    assert by[ids["Панель"]]["checks"] == 3 and by[ids["Панель"]]["down"] == 1
    assert by[ids["KZ"]]["checks"] == 3 and by[ids["KZ"]]["down"] == 2 and not by[ids["KZ"]]["broken"]
    assert by[ids["KZ"]]["avg_ms"] == 1000 and by[ids["KZ"]]["up_pct"] == pytest.approx(33.3)
    # все отсюда недоступны, а напрямую часть отвечает - сломан сам прокси
    assert by[ids["RU"]]["broken"] and by[ids["RU"]]["error"] == "прокси не отвечает"
    assert by[ids["Порты"]]["checks"] == 0
    assert d["ports_location_id"] == ids["Порты"]
    # в таблице: blog лежит везде, shop недоступен из KZ; api - только из сломанного прокси, его нет
    rows = {x["name"]: x for x in d["sites"]}
    assert set(rows) == {"shop", "blog"}
    assert rows["shop"]["main"]["status"] == "up"
    assert rows["shop"]["cells"][str(ids["KZ"])]["status"] == "down"
    assert rows["blog"]["check_id"] == checks["blog"]


async def test_location_exit_saved(client: httpx.AsyncClient, auth_headers, monkeypatch):
    """Выход в интернет: из проверки при создании, при смене адреса без проверки сбрасывается,
    проверка сохраненной локации запоминает его заново."""
    r = await client.post("/api/locations", json={"name": "KZ", "url": "http://203.0.113.10:3128",
                                                  "exit_ip": "203.0.113.10", "exit_country": "kz"},
                          headers=auth_headers)
    assert r.status_code == 201
    loc = r.json()
    assert loc["exit_country"] == "KZ" and loc["exit_ip"] == "203.0.113.10" and loc["exit_at"]

    r = await client.patch(f"/api/locations/{loc['id']}", json={"url": "http://203.0.113.11:3128"},
                           headers=auth_headers)
    assert r.json()["exit_country"] == "" and r.json()["exit_at"] is None

    async def fake_test(url, timeout_s=10.0):
        return True, "прокси работает", 120

    async def fake_exit(url, timeout_s=6.0):
        assert url == "http://203.0.113.11:3128"
        return "203.0.113.11", "DE"

    monkeypatch.setattr("app.checks.test_proxy", fake_test)
    monkeypatch.setattr("app.checks.proxy_exit", fake_exit)
    r = await client.post(f"/api/locations/{loc['id']}/test", headers=auth_headers)
    assert r.status_code == 200 and r.json()["exit_country"] == "DE"
    r = await client.get("/api/locations", headers=auth_headers)
    assert r.json()[0]["exit_country"] == "DE" and r.json()[0]["exit_ip"] == "203.0.113.11"

    # проверка нового адреса тоже говорит, где выход
    r = await client.post("/api/locations/test", json={"url": "http://203.0.113.11:3128"}, headers=auth_headers)
    assert r.json()["exit_ip"] == "203.0.113.11"


async def test_proxy_exit_parses_trace(monkeypatch):
    """Cloudflare trace: адрес и страна; без loc= страна по офлайн-таблице, сбой - пусто."""
    from app import checks as ce

    class Resp:
        def __init__(self, text):
            self.text = text

    def client_with(text=None, exc=None):
        class C:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                assert "cdn-cgi/trace" in url
                if exc:
                    raise exc
                return Resp(text)
        return C

    monkeypatch.setattr("app.checks.httpx.AsyncClient", client_with("fl=1\nip=203.0.113.10\nloc=KZ\n"))
    assert await ce.proxy_exit("http://203.0.113.10:3128") == ("203.0.113.10", "KZ")
    monkeypatch.setattr("app.geoip.country_of", lambda ip: "FI")
    monkeypatch.setattr("app.checks.httpx.AsyncClient", client_with("ip=203.0.113.11\nloc=XX\n"))
    assert await ce.proxy_exit("") == ("203.0.113.11", "FI")
    monkeypatch.setattr("app.checks.httpx.AsyncClient", client_with(exc=httpx.ConnectError("refused")))
    assert await ce.proxy_exit("http://203.0.113.10:3128") == ("", "")


async def test_broken_proxy_one_alert(tmp_path, monkeypatch):
    """Прокси лег целиком: один алерт про прокси после выдержки, без "недоступен из ..." по
    каждому сайту, и отбой, когда заработал. Если сам прокси отвечает, это блокировка сайтов в
    той стране - тогда обычные алерты по сайтам."""
    from datetime import datetime, timedelta, timezone

    collector._proxy_confirm.clear()
    db = (tmp_path / "bp.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        for n in ("a", "b", "c"):
            s.add(Check(name=n, type="http", target=f"https://{n}.example",
                        enabled=True, check_locations=True, last_status="up"))
        s.add(Location(name="Прямая", url="", enabled=True))
        s.add(Location(name="KZ", url="http://203.0.113.10:3128", enabled=True))
        await s.commit()
    async with factory() as s:
        proxy = (await s.scalars(select(Location).where(Location.url != ""))).one()
        for cid in await s.scalars(select(Check.id)):
            s.add(LocationResult(check_id=cid, location_id=proxy.id, status="down",
                                 message="proxy: connect timeout", consecutive_fails=5))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    proxy_ok = {"v": False}
    tested: list[str] = []

    async def fake_test(url, timeout_s=10.0):
        tested.append(url)
        return proxy_ok["v"], "x", None

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr("app.checks.test_proxy", fake_test)
    settings = Settings(alert_webhook="http://hook", panel_url="https://p.example.com/")
    now = datetime.now(timezone.utc)

    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == [] and tested == ["http://203.0.113.10:3128"]  # выдержка еще идет

    # после выдержки - один алерт про прокси, по сайтам ничего
    now += timedelta(seconds=collector._LOC_PARTIAL_SUSTAIN + 30)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert len(sent) == 1, sent
    assert "Прокси локации «KZ» не отвечает" in sent[0] and "connect timeout" in sent[0]

    # тот же сломанный прокси - без повтора
    sent.clear()
    now += timedelta(seconds=60)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent == []
    assert len(tested) == 2  # ответ прокси помним 5 минут, не дергаем каждый тик

    # прокси заработал - отбой
    async with factory() as s:
        for r in await s.scalars(select(LocationResult)):
            r.status, r.consecutive_fails = "up", 0
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    assert len(sent) == 1 and "снова работает" in sent[0]

    # все недоступны, но сам прокси отвечает - это блокировка сайтов, алерты по сайтам
    sent.clear()
    collector._proxy_confirm.clear()
    proxy_ok["v"] = True
    async with factory() as s:
        for r in await s.scalars(select(LocationResult)):
            r.status, r.consecutive_fails = "down", 5
        await s.commit()
    await collector.evaluate_location_alerts(factory, settings, now)
    now += timedelta(seconds=collector._LOC_PARTIAL_SUSTAIN + 30)
    await collector.evaluate_location_alerts(factory, settings, now)
    assert sent and all("Прокси локации" not in t for t in sent)
    assert any("🔴 KZ — недоступен" in t for t in sent)
    await engine.dispose()


def test_broken_location_needs_three_checks():
    """Два недоступных сайта - еще не "сломан прокси": это может быть блокировка обоих."""
    loc = SimpleNamespace(id=2, url="http://203.0.113.10:3128")
    mk = lambda i: SimpleNamespace(id=i, enabled=True, check_locations=True, type="http",  # noqa: E731
                                   location_ids=None, last_status="up")
    res = lambda ids: {(i, 2): SimpleNamespace(status="down", consecutive_fails=3) for i in ids}  # noqa: E731
    two = [mk(1), mk(2)]
    assert collector.broken_location_ids([loc], two, res([1, 2])) == set()
    three = two + [mk(3)]
    assert collector.broken_location_ids([loc], three, res([1, 2, 3])) == {2}
    # с панели тоже все лежат - это беда сайтов, а не прокси
    for c in three:
        c.last_status = "down"
    assert collector.broken_location_ids([loc], three, res([1, 2, 3])) == set()
