"""Износ SSD: когда диск дойдет до 90% и до 100% (app/disk_wear.py)."""
from datetime import datetime, timedelta, timezone

from app import collector, disk_wear
from app.db import Base, create_engine_and_factory
from app.models import Server

NOW = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)


def _block(*disks) -> dict:
    return {"v": 1, "ts": int(NOW.timestamp()), "virt": "none", "raid": [], "missing": [],
            "io": {"count": 0, "last": []}, "disks": list(disks)}


def _disk(ser, wear, poh=None, dev="nvme0n1") -> dict:
    return {"dev": dev, "model": "SSD", "serial": ser, "ok": True, "health": "PASSED", "wear": wear,
            "poh": poh, "grow": {}}


def test_lifetime_then_measured_rate():
    # первое наблюдение без наработки - скорость неизвестна
    h = disk_wear.update(None, _block(_disk("A", 50)), NOW)
    assert h["A"]["rate"] is None and h["A"]["d90"] is None
    # по средней за жизнь: 50% за 500 суток
    h = disk_wear.update(None, _block(_disk("A", 50, poh=500 * 24)), NOW)
    assert h["A"]["src"] == "lifetime" and abs(h["A"]["rate"] - 0.1) < 1e-4
    assert h["A"]["d90"] == (NOW.date() + timedelta(days=400)).isoformat()
    assert h["A"]["d100"] == (NOW.date() + timedelta(days=500)).isoformat()
    # первая смена через 3 дня, вторая еще через 12: замер 1% за 12 суток, первое наблюдение не в счет
    h = disk_wear.update(h, _block(_disk("A", 51, poh=500 * 24)), NOW + timedelta(days=3))
    assert h["A"]["src"] == "lifetime" and len(h["A"]["t"]) == 2
    h = disk_wear.update(h, _block(_disk("A", 51, poh=500 * 24)), NOW + timedelta(days=9))
    assert len(h["A"]["t"]) == 2  # процент не сменился - новой точки нет
    h = disk_wear.update(h, _block(_disk("A", 52, poh=500 * 24)), NOW + timedelta(days=15))
    assert h["A"]["src"] == "measured" and abs(h["A"]["rate"] - 1 / 12) < 1e-4


def test_soon_with_hysteresis_and_forget():
    day = lambda n: NOW + timedelta(days=n)  # noqa: E731
    # 85%, по замеру 0.1%/сутки: до 90% 50 суток - пора
    h = {"A": {"t": [["2026-09-01", 83], ["2026-09-10", 84], ["2026-09-20", 85]], "seen": "2026-10-16"}}
    h = disk_wear.update(h, _block(_disk("A", 85)), day(0))
    assert h["A"]["src"] == "measured" and h["A"]["soon"] is True
    # скорость упала: до 90% 80 суток - еще держим, 100 - снимаем
    h["A"]["t"] = [["2026-07-01", 83], ["2026-08-01", 84], ["2026-08-17", 85]]
    h = disk_wear.update(h, _block(_disk("A", 85)), day(0))
    assert abs(h["A"]["rate"] - 1 / 16) < 1e-4 and h["A"]["soon"] is True
    h["A"]["t"] = [["2026-07-01", 83], ["2026-08-01", 84], ["2026-08-21", 85]]
    h = disk_wear.update(h, _block(_disk("A", 85)), day(0))
    assert h["A"]["soon"] is False
    # диск пропал из отчета: месяц помним, потом забываем
    h2 = disk_wear.update(h, _block(), day(10))
    assert "A" in h2
    assert "A" not in disk_wear.update(h, _block(), day(40))


def test_alert_line_and_twin_hint():
    hist = {ser: {"wear": 80, "rate": 0.2, "src": "measured", "d90": "2026-12-06", "soon": True}
            for ser in ("A", "B")}
    hist["C"] = {"wear": 92, "rate": 0.2, "src": "measured", "d90": None, "soon": True}
    block = _block(_disk("A", 80), _disk("B", 80, dev="nvme1n1"), _disk("C", 92, dev="sda"))
    probs = collector.disk_health_problems(block, wear_hist=hist)
    soon = [p for p in probs if p[1].startswith("wearsoon:")]
    assert [p[0] for p in soon] == [1, 1]
    assert "90% примерно к 06.12.2026 (0.20% в сутки, по замеру)" in soon[0][2]
    assert any(p[1] == "" and "менять по одному" in p[2] for p in probs)
    # 92%: уже своя строка "износ ... пора планировать замену", прогноз до 90 не нужен
    assert not any(p[1] == "wearsoon:C" for p in probs) and any(p[1] == "wear:C" for p in probs)
    short = collector.disk_health_problems(block, short=True, wear_hist=hist)
    assert not any("менять по одному" in p[2] for p in short)
    assert any(p[2] == "диск nvme0n1: износ 80%, 90% примерно к 06.12.2026" for p in short)
    # без истории - как раньше
    assert not any(p[1].startswith("wearsoon:") for p in collector.disk_health_problems(block))


async def test_refresh_writes_history(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'w.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    rep = {"clock_unix": NOW.timestamp(), "extras": {"disk-health": _block(_disk("A", 77, poh=5000 * 24))}}
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, last_report=rep))
        s.add(Server(name="web-b", token_hash="y", enabled=True, last_report={}))
        await s.commit()
    disk_wear._last_run = None
    await disk_wear.refresh(factory, NOW, collector.disk_health_block)
    async with factory() as s:
        a, b = await s.get(Server, 1), await s.get(Server, 2)
        assert a.disk_wear["A"]["wear"] == 77 and a.disk_wear["A"]["src"] == "lifetime"
        assert b.disk_wear is None
        card = disk_wear.card(collector.disk_health_block(a.last_report), a.disk_wear)
    assert card[0]["dev"] == "nvme0n1" and card[0]["d90"] and card[0]["soon"] is False
    await engine.dispose()


async def test_servers_api_with_wear_history(client, auth_headers):
    """servers.disk_wear (словарь по серийникам) и прогноз в ответе API (список) - разные поля:
    одноименное поле ответа роняло /api/servers на первом же сервере с историей."""
    f = client._transport.app.state.session_factory  # noqa: SLF001
    block = _block(_disk("A", 77, poh=5000 * 24), _disk("B", 77, poh=5000 * 24, dev="nvme1n1"))
    async with f() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True,
                     last_report={"clock_unix": NOW.timestamp(), "extras": {"disk-health": block}},
                     disk_wear=disk_wear.update(None, block, NOW)))
        await s.commit()
    r = await client.get("/api/servers", headers=auth_headers)
    assert r.status_code == 200
    srv = [x for x in r.json() if x["name"] == "web-a"][0]
    assert [w["dev"] for w in srv["wear_forecast"]] == ["nvme0n1", "nvme1n1"]
    assert "disk_wear" not in srv


def test_writer_line_next_to_wear():
    # кто больше всех пишет: темп за окно растягивается на сутки, меньше 50 ГБ в сутки - не повод
    big = {"kind": "container", "name": "pg", "w": 40 * 10**9, "r": 0, "s": 3600}
    assert disk_wear.writer_text([{"kind": "unit", "name": "x.service", "w": 10**9, "r": 0, "s": 3600}, big]) == (
        "больше всех на диск пишет контейнер pg: около 894 ГБ в сутки")
    assert disk_wear.writer_text([{"kind": "unit", "name": "user.slice", "w": 3 * 10**9, "r": 0, "s": 1800}]) == (
        "больше всех на диск пишет сессии и cron (user.slice): около 134 ГБ в сутки")
    assert disk_wear.writer_text([{"kind": "pod", "name": "a/b", "w": 10**9, "r": 0, "s": 3600}]) == ""
    assert disk_wear.writer_text(None) == "" and disk_wear.writer_text(["x", {"w": "?"}]) == ""
    hist = {"A": {"wear": 80, "rate": 0.2, "src": "measured", "d90": "2026-12-06", "soon": True}}
    block = _block(_disk("A", 80))
    probs = collector.disk_health_problems(block, wear_hist=hist, writers=[big])
    assert (1, "", "больше всех на диск пишет контейнер pg: около 894 ГБ в сутки") in probs
    # в сводку на главной не идет, без скорого износа тоже
    assert not any("пишет" in p[2] for p in collector.disk_health_problems(block, short=True, wear_hist=hist, writers=[big]))
    assert not any("пишет" in p[2] for p in collector.disk_health_problems(block, writers=[big]))
