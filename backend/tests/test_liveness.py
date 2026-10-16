"""Недоступен: молчит агент или сам сервер (app/liveness.py) - панель стучится к серверу сама."""
from datetime import datetime, timedelta, timezone

from app import collector, liveness, open_ports
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server

SSH_NODE = "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5"


def _report(**kw) -> dict:
    rep = {"ips": ["203.0.113.10", "10.0.0.5"],
           "extras": {"changes": {"ssh": {"ports": [2221], "banner": SSH_NODE}}}}
    rep.update(kw)
    return rep


def _exposure() -> dict:
    return {"ts": "2026-10-17T00:00:00+00:00", "via": "панель", "own": True,
            "ips": ["203.0.113.10", "203.0.113.99"],
            "ports": [{"ip": "203.0.113.10", "port": 443, "state": "open"},
                      {"ip": "203.0.113.10", "port": 2221, "state": "open"},
                      {"ip": "203.0.113.99", "port": 22, "state": "open"},
                      {"ip": "203.0.113.10", "port": 5432, "state": "filtered"}],
            # на внешнем адресе 203.0.113.99 отвечает чужой SSH: это шлюз NAT
            "banners": {"203.0.113.99:22": "SSH-2.0-OpenSSH_7.4"}}


def test_candidates_skip_foreign_and_private():
    s = Server(name="web-a", last_report=_report(), exposure=_exposure(), agent_ip="192.0.2.7")
    got = liveness.candidates(s)
    assert ("203.0.113.10", 443, False) in got and ("203.0.113.10", 2221, True) in got
    assert all(ip != "203.0.113.99" for ip, _p, _c in got)  # чужой хост за этим адресом
    assert all(ip != "10.0.0.5" for ip, _p, _c in got)      # частный адрес с интерфейса - нет
    assert ("192.0.2.7", 2221, True) in got                 # заданный вручную - с баннером
    assert liveness.candidates(Server(name="x", last_report={})) == []


async def test_check_alive_refused_silent(monkeypatch):
    s = Server(name="web-a", last_report=_report(), exposure=_exposure())
    answers: dict[tuple[str, int], str] = {}
    banner = {"v": SSH_NODE}

    async def fake_probe(ip, port, proxy):
        return answers.get((ip, port), "filtered")

    async def fake_banner(ip, port, proxy):
        return banner["v"]

    monkeypatch.setattr(open_ports, "probe", fake_probe)
    monkeypatch.setattr(open_ports, "ssh_banner", fake_banner)
    assert (await liveness.check(s))["state"] == "silent"
    answers[("203.0.113.10", 443)] = "closed"
    r = await liveness.check(s)
    assert r["state"] == "refused" and r["where"] == "203.0.113.10:443"
    answers[("203.0.113.10", 2221)] = "open"
    r = await liveness.check(s)
    assert r["state"] == "alive" and r["where"] == "203.0.113.10:2221"
    assert "молчит агент" in liveness.text(r)
    # по адресу отвечает SSH другой версии - это не наша машина
    banner["v"] = "SSH-2.0-OpenSSH_8.2p1"
    assert (await liveness.check(s))["state"] == "refused"
    assert "файрволом" in liveness.text({"state": "silent", "tried": ["203.0.113.10:443"]})


async def test_offline_alert_says_whether_server_answers(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report=_report(), exposure=_exposure(),
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
                     offline_after_seconds=120, alert_sustain_seconds=0,
                     created_at=now - timedelta(days=1)))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    async def fake_probe(ip, port, proxy):
        return "open" if (ip, port) == ("203.0.113.10", 2221) else "filtered"

    async def fake_banner(ip, port, proxy):
        return SSH_NODE

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr(open_ports, "probe", fake_probe)
    monkeypatch.setattr(open_ports, "ssh_banner", fake_banner)
    settings = Settings(alert_webhook="http://hook")
    later = now + timedelta(minutes=30)
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 1 and "с панели сервер отвечает (203.0.113.10:2221)" in sent[0]
    async with factory() as s:
        st = (await s.get(Server, 1)).alert_state
        assert st["offline_probe"]["state"] == "alive"
    # вернулся - отбой, отметка проверки снята
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = later
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 2
    async with factory() as s:
        assert (await s.get(Server, 1)).alert_state.get("offline_probe") is None
    await engine.dispose()


async def test_baseline_tells_firewall_from_down(tmp_path, monkeypatch):
    """Эталон: пока сервер на связи, панель проверяет, достукивается ли до него. Не достукивалась -
    молчание в момент алерта ничего не значит; достукивалась - лежит сервер или сеть."""
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, last_seen=now,
                     last_report=_report(), exposure=_exposure()))
        s.add(Server(name="web-b", token_hash="y", enabled=True, last_seen=now,
                     last_report=_report(ips=["203.0.113.20"]), exposure=None))
        # молчит давно - эталон ему не снимаем
        s.add(Server(name="web-c", token_hash="z", enabled=True, last_seen=now - timedelta(hours=1),
                     last_report=_report(ips=["203.0.113.30"]), exposure=None))
        await s.commit()
    up = {("203.0.113.10", 2221)}

    async def fake_probe(ip, port, proxy):
        return "open" if (ip, port) in up else "filtered"

    async def fake_banner(ip, port, proxy):
        return SSH_NODE

    monkeypatch.setattr(open_ports, "probe", fake_probe)
    monkeypatch.setattr(open_ports, "ssh_banner", fake_banner)
    assert await liveness._base_due(factory, now) == 2
    assert await liveness._base_due(factory, now + timedelta(hours=1)) == 0  # свежий - не трогаем
    async with factory() as s:
        a, b, c = [await s.get(Server, i) for i in (1, 2, 3)]
        assert a.liveness_base["state"] == "alive" and a.liveness_base["where"] == "203.0.113.10:2221"
        assert a.liveness_base["v"] == liveness.BASE_V
        assert b.liveness_base["state"] == "silent" and c.liveness_base is None
    # что отвечало на связи - первым в очереди
    assert liveness.candidates(a)[0] == ("203.0.113.10", 2221, True)
    # сервер замолчал целиком
    up.clear()
    r = await liveness.check(a)
    assert r["state"] == "silent" and r["was"] == "203.0.113.10:2221"
    assert "хотя на связи отвечал" in liveness.text(r) and "файрволом" not in liveness.text(r)
    r = await liveness.check(b)
    assert r["state"] == "blind" and "проверить с панели нельзя" in liveness.text(r)
    # эталон мог попасть на сбой сети: если сервер ответил, это важнее
    up.add(("203.0.113.20", 2221))
    assert (await liveness.check(b))["state"] == "alive"
    await engine.dispose()


def test_shared_address_counts_only_ssh():
    """Общий фронт перед двумя серверами отвечает на 443 и тогда, когда один из них лежит."""
    ex = {"ts": "2026-10-17T00:00:00+00:00", "via": "панель", "own": True, "ips": ["203.0.113.50"],
          "ports": [{"ip": "203.0.113.50", "port": 443, "state": "open"},
                    {"ip": "203.0.113.50", "port": 2221, "state": "open"}]}
    a = Server(id=1, name="app-a", last_report=_report(ips=["10.0.0.5"]), exposure=ex)
    b = Server(id=2, name="app-b", last_report=_report(ips=["10.0.0.6"]), exposure=ex)
    c = Server(id=3, name="web-c", last_report=_report(ips=["203.0.113.60"]), exposure=None)
    common = liveness.shared([a, b, c])
    assert common[1] == {"203.0.113.50"} and common[3] == frozenset()
    assert liveness.candidates(a) == [("203.0.113.50", 443, False), ("203.0.113.50", 2221, True)]
    assert liveness.candidates(a, common[1]) == [("203.0.113.50", 2221, True)]
    # эталон 1.4.244 (без версии) мог судить по общему адресу - его как будто нет
    assert liveness.reachable({"state": "alive", "where": "203.0.113.50:443"}) is None
    assert liveness.reachable({"state": "silent", "where": "", "v": liveness.BASE_V}) is False
