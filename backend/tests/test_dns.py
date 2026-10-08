"""DNS ноды (агент 2.25): что считать проблемой, выдержка алерта, подсказка про сбой у провайдера."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collector


def _rep(ts, servers, miss_ms=40, miss_err="", miss_age=60, age=10):
    return {"clock_unix": ts, "dns": {
        "mode": "resolved", "ts": ts - age, "servers": servers,
        "miss_ms": miss_ms, "miss_err": miss_err, "miss_ts": ts - miss_age}}


def test_dns_problem_is_what_programs_would_feel():
    ts = datetime(2026, 10, 8, 20, tzinfo=timezone.utc).timestamp()
    ok = [{"addr": "185.12.64.1", "ms": 4}, {"addr": "185.12.64.2", "ms": 6}]
    assert collector.dns_problem(_rep(ts, ok)) == ""
    # имя не из кэша резолвится 4,8 с - это и есть сбой Hetzner 08.10
    assert collector.dns_problem(_rep(ts, ok, miss_ms=4800)) == "имя не из кэша резолвится 4800 мс"
    assert collector.dns_problem(_rep(ts, ok, miss_ms=-1, miss_err="timeout")) == \
        "имя не из кэша не резолвится (нет ответа)"
    # все резолверы разом - проблема, даже если последний замер не из кэша был в норме
    slow = [{"addr": "185.12.64.1", "ms": -1, "err": "timeout"}, {"addr": "185.12.64.2", "ms": 3900}]
    assert collector.dns_problem(_rep(ts, slow)) == "резолверы: 185.12.64.1 нет ответа, 185.12.64.2 3900 мс"
    # один медленный из двух при нормальном резолве - не проблема, resolved уйдет на соседний
    half = [{"addr": "185.12.64.1", "ms": 3900}, {"addr": "185.12.64.2", "ms": 5}]
    assert collector.dns_problem(_rep(ts, half)) == ""
    # снимок старше 5 минут или старый замер не из кэша - не судим
    assert collector.dns_problem(_rep(ts, slow, age=600)) == ""
    assert collector.dns_problem(_rep(ts, ok, miss_ms=4800, miss_age=3600)) == ""
    assert collector.dns_problem({"clock_unix": ts}) == ""


def test_dns_condition_waits_ten_minutes_and_names_the_provider():
    now = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
    ts = now.timestamp()
    slow = [{"addr": "185.12.64.1", "ms": 4800}, {"addr": "185.12.64.2", "ms": -1, "err": "timeout"}]

    def srv(sid, state):
        from app.models import Server
        return Server(id=sid, name=f"n{sid}", last_seen=now, last_report=_rep(ts, slow), alert_state=state,
                      offline_after_seconds=120, enabled=True)

    first = collector._server_conditions(srv(1, {}), now)["dns"]
    assert first[0] == 0 and first[1]["since"] == now.isoformat()
    held = collector._server_conditions(srv(1, {"dns_since": (now - timedelta(minutes=11)).isoformat()}), now)["dns"]
    assert held[0] == 2 and held[1]["details"].startswith("резолверы: 185.12.64.1 4800 мс")
    slow_by = {1: {"185.12.64.1", "185.12.64.2"}, 2: {"185.12.64.1"}, 3: {"185.12.64.2"}, 4: {"1.1.1.1"}}
    assert collector.dns_others_hint(1, slow_by) == \
        ". Так же у 2 других нод с теми же резолверами, похоже на сбой у провайдера"
    assert collector.dns_others_hint(4, slow_by) == ""
    assert collector.dns_others_hint(1, {1: {"185.12.64.1"}, 2: {"185.12.64.1"}}) == ""  # одна соседняя - еще не повод


async def test_dns_alert_fires_after_hold_with_the_provider_hint(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    slow = [{"addr": "185.12.64.1", "ms": 4800}, {"addr": "185.12.64.2", "ms": 5200}]
    since = (now - timedelta(minutes=11)).isoformat()
    async with factory() as s:
        for i in range(3):
            s.add(Server(name=f"node-{i}", token_hash=f"t{i}", enabled=True, backup_not_required=True,
                         last_seen=now, last_report={"cpu_percent": 5, **_rep(ts, slow)},
                         alert_state={"dns_since": since},
                         cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    hits = [x for x in sent if "DNS" in x or "резолвер" in x]
    assert hits, sent
    assert any("Так же у 2 других нод" in x for x in hits), hits
    await engine.dispose()


def test_dns_in_what_is_broken_after_the_hold():
    now = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
    ts = now.timestamp()
    slow = [{"addr": "185.12.64.1", "ms": 4800}]
    s = SimpleNamespace(last_seen=now, offline_after_seconds=120, last_report=_rep(ts, slow),
                        alert_state={"dns_since": (now - timedelta(minutes=3)).isoformat()},
                        disk_forecast=None, alert_mutes=[], alert_snoozes={},
                        disk_crit_percent=0, disk_alert_percent=0, disk_warn_percent=0)
    assert [p for p in collector.server_problems(s, now) if p["kind"] == "dns"] == []
    s.alert_state = {"dns_since": (now - timedelta(minutes=12)).isoformat()}
    got = [p for p in collector.server_problems(s, now) if p["kind"] == "dns"]
    assert got and got[0]["level"] == 2 and got[0]["text"] == "DNS: резолверы: 185.12.64.1 4800 мс"
