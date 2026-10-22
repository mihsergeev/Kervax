"""Сервер лег вместе со своими сайтами: одно сообщение "не на связи" с сайтами строкой, а не
по сообщению на сайт раньше него. Отбой - тоже одно: сервер вернулся, какие сайты поднялись."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collector, parents
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, Server

SRV_IP = "203.0.113.10"
OUT_IP = "198.51.100.7"


def _srv(i, name, sites=(), ip=""):
    return SimpleNamespace(id=i, name=name, parent_id=None, external_ip=ip, agent_ip="",
                           last_report={"web_services": [{"sites": list(sites)}]})


def _chk(target, **kw):
    base = dict(target=target, type="http", probe_local=False, probe_server_id=None, parent_mode="auto",
                parent_server_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_domain_belongs_to_the_server_its_dns_points_to():
    idx = parents.Index([
        _srv(1, "web-a", ["a.example", "d.example"], "203.0.113.20"),
        _srv(2, "web-b", [], SRV_IP),
    ])
    idx.resolved = {
        "a.example": frozenset({SRV_IP}),
        "b.example": frozenset({SRV_IP}),
        "c.example": frozenset({SRV_IP, OUT_IP}),
        "d.example": frozenset({OUT_IP}),
    }
    # сайт остался в конфиге web-a, а запросы идут на web-b
    assert parents.parents_of(_chk("https://a.example"), idx) == [(2, "ip")]
    # веб-сервер web-b агент не разбирает (панель хостинга): сайт находится по DNS
    assert parents.parents_of(_chk("b.example:443", type="tcp_port"), idx) == [(2, "ip")]
    # второй адрес не из панели - балансировка, сам сервер сайт целиком не роняет
    assert parents.parents_of(_chk("https://c.example"), idx) == []
    # за адресом не из панели (CDN) - как раньше, по конфигу веб-сервера
    assert parents.parents_of(_chk("https://d.example"), idx) == [(1, "site")]
    # ручная настройка важнее
    assert parents.parents_of(_chk("https://a.example", parent_mode="off"), idx) == []


async def test_resolver_answers_are_cached_and_survive_a_failure(monkeypatch):
    calls: list[str] = []
    answer = {"a.example": frozenset({SRV_IP})}

    async def lookup(host):
        calls.append(host)
        if host not in answer:
            raise OSError("нет ответа")
        return answer[host]

    monkeypatch.setattr(parents, "_lookup", lookup)
    idx = parents.Index([_srv(1, "web-a", [], SRV_IP)])
    checks = [_chk("https://a.example"), _chk("https://a.example/x"), _chk(SRV_IP), _chk("z.example", type="dns")]
    await parents.resolve_for(idx, checks)
    assert calls == ["a.example"] and idx.resolved == {"a.example": frozenset({SRV_IP})}
    await parents.resolve_for(parents.Index([]), checks)
    assert calls == ["a.example"]  # из кеша
    # резолвер панели сломался: прошлый адрес остается
    answer.clear()
    monkeypatch.setattr(parents, "_dns", {"a.example": (frozenset({SRV_IP}), 0.0)})
    idx = await parents.resolve_for(parents.Index([]), checks)
    assert idx.resolved == {"a.example": frozenset({SRV_IP})}


def test_late_server_is_a_reason_to_wait():
    now = datetime.now(timezone.utc)
    srvs = [SimpleNamespace(id=1, enabled=True, last_seen=now - timedelta(seconds=20), offline_after_seconds=120),
            SimpleNamespace(id=2, enabled=True, last_seen=now - timedelta(seconds=70), offline_after_seconds=120),
            SimpleNamespace(id=3, enabled=True, last_seen=None, offline_after_seconds=120),
            SimpleNamespace(id=4, enabled=False, last_seen=now - timedelta(hours=1), offline_after_seconds=120)]
    assert parents.late_ids(srvs, now, False) == {2}
    assert parents.late_ids(srvs, now, True) == set()


# ---------------------------------------------------------------- сборщик


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


def _server(name, seen, **kw):
    base = dict(name=name, token_hash="t-" + name, enabled=True, backup_not_required=True, last_seen=seen,
                last_report={}, alert_state={}, cpu_alert_percent=0, mem_alert_percent=0,
                disk_alert_percent=0, offline_after_seconds=120, alert_sustain_seconds=0)
    base.update(kw)
    return Server(**base)


def _capture(monkeypatch):
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    return sent


def _bad(name, cid):
    return ("bad", name, "down", "сервер не принимает соединение: 10 с без ответа", None, cid, None)


async def test_server_down_with_its_sites_is_one_message(tmp_path, monkeypatch):
    async def lookup(host):
        return frozenset({SRV_IP}) if host in ("a.example", "b.example") else frozenset({OUT_IP})

    monkeypatch.setattr(parents, "_lookup", lookup)
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_server("web-a", now - timedelta(seconds=70), external_ip=SRV_IP))
        a = Check(name="site-a", type="http", target="https://a.example", last_status="down", last_checked_at=now)
        b = Check(name="site-b", type="http", target="https://b.example", last_status="down", last_checked_at=now)
        c = Check(name="site-c", type="http", target="https://c.example", last_status="down", last_checked_at=now)
        s.add_all([a, b, c])
        await s.commit()
        aid, bid, cid = a.id, b.id, c.id
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook", panel_url="https://panel.example")

    # сервер опоздал с отчетом на минуту: его сайты, упавшие в эту минуту, ждут; чужой - алертит
    await collector._send_alerts(factory, settings, [_bad("site-a", aid), _bad("site-b", bid),
                                                      _bad("site-c", cid)], now)
    assert [m.target for m in sent] == ["site-c"]

    # сервер так и не ответил: одно сообщение, сайты строкой ссылками на мониторы
    later = now + timedelta(minutes=5)
    sent.clear()
    await collector._send_alerts(factory, settings, [_bad("site-a", aid), _bad("site-b", bid)], later)
    await collector.evaluate_servers(factory, settings, later)
    assert [(m.kind, m.target) for m in sent] == [("offline", "web-a")], [str(m) for m in sent]
    msg = str(sent[0])
    assert msg.startswith("🔴📡 ")
    assert ('\nСайты: <a href="https://panel.example/?check=%d">site-a</a>, '
            '<a href="https://panel.example/?check=%d">site-b</a>' % (aid, bid)) in msg
    assert "site-c" not in msg

    # сервер вернулся: отбой ждет, пока сайты проверят после возврата
    back = later + timedelta(minutes=2)
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = back
        await s.commit()
    sent.clear()
    await collector.evaluate_servers(factory, settings, back)
    assert not [m for m in sent if m.kind == "offline"], [str(m) for m in sent]

    # site-a поднялся, site-b еще нет: одно "снова на связи" с обоими
    async with factory() as s:
        (await s.get(Check, aid)).last_status = "up"
        for i in (aid, bid):
            (await s.get(Check, i)).last_checked_at = back + timedelta(seconds=40)
        (await s.get(Server, 1)).last_seen = back + timedelta(seconds=60)
        await s.commit()
    await collector.evaluate_servers(factory, settings, back + timedelta(seconds=60))
    rec = [m for m in sent if m.kind == "offline"]
    assert len(rec) == 1 and rec[0].recovery, [str(m) for m in sent]
    text = str(rec[0])
    assert "снова на связи" in text
    assert '\nПоднялись сайты: <a href="https://panel.example/?check=%d">site-a</a>' % aid in text
    assert '\nЕще поднимаются: <a href="https://panel.example/?check=%d">site-b</a>' % bid in text
    async with factory() as s:
        st = (await s.get(Server, 1)).alert_state
    assert not st.get("offline") and not st.get("offline_kids") and not st.get("offline_back")
    await engine.dispose()


async def test_recovery_does_not_wait_forever_for_a_site(tmp_path, monkeypatch):
    """Сайт после возврата сервера не проверили (монитор выключили, интервал большой): отбой
    приходит не позже _KIDS_WAIT."""
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_server("web-a", now, last_report={"web_services": [{"sites": ["a.example"]}]},
                      alert_state={"offline": 1, "offline_said": 1, "offline_kids": [1]}))
        s.add(Check(name="site-a", type="http", target="https://a.example", last_status="down",
                    last_checked_at=now - timedelta(minutes=10)))
        await s.commit()
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert not [m for m in sent if m.kind == "offline"]
    t2 = now + timedelta(seconds=collector._KIDS_WAIT + 5)
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = t2
        await s.commit()
    await collector.evaluate_servers(factory, settings, t2)
    rec = [str(m) for m in sent if m.kind == "offline"]
    assert len(rec) == 1 and "снова на связи" in rec[0] and "Еще поднимаются: site-a" in rec[0], rec
    await engine.dispose()
