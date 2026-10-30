"""Алерт gre_down (app/gre.py down): сервер жив, а GRE до панели не работает.

Агент 2.56 при неответившем туннеле уходит напрямую через 4 с и 2 минуты туннель не пробует, helper
gre-setup поднимает его снова раз в минуту: одиночный срыв - не алерт. Алерт - когда агент ходит
напрямую 10 минут подряд (или хост панели не поднял свою сторону), отбой - когда снова туннелем.
Сервер молчит целиком - про него offline, этот не шлем."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector, gre, srv_short
from app.models import Server

pytestmark = pytest.mark.calm

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def _srv(via="direct", agent_at=T0 - timedelta(hours=1), **rep) -> Server:
    s = Server(name="web-a", gre_enabled=True, gre_net="10.254.0.0/30", gre_peer="203.0.113.10",
               gre_agent_at=agent_at, gre_seen_at=T0, gre_state={"up": True},
               last_seen=T0, last_report={"gre_path": {"via": via, "since": int(T0.timestamp())}, **rep})
    s.id = 7
    return s


def _host(ok=True, err="", ts=None):
    return {"fw": "ufw", "ts": int((ts or datetime.now(timezone.utc)).timestamp()),
            "ifaces": {"kvxg7": {"peer": "203.0.113.10", "local": "198.51.100.5", "ok": ok, "error": err}}}


def test_direct_after_the_tunnel_worked_is_down():
    d = gre.down(_srv(connect="10.254.0.1", connect_down=True), T0, _host())
    assert (d["level"], d["why"], d["direct"]) == (2, "connect_down", True)
    assert d["text"] == "GRE до панели не работает, агент ходит напрямую: туннель не ответил агенту"
    # helper сам снял connect=, потому что панель через туннель не отвечает
    d = gre.down(_srv(), T0, _host())
    assert d["why"] == "up" and "агент в него не перешел" in d["text"]
    s = _srv()
    s.gre_state = {"up": False, "error": "the panel does not answer through the tunnel (10.254.0.1:443)"}
    assert "ошибка GRE: the panel does not answer" in gre.down(s, T0, _host())["text"]
    s.gre_seen_at = T0 - timedelta(minutes=20)
    assert gre.down(s, T0, _host())["why"] == "stale"


def test_not_down_while_on_the_tunnel_or_never_worked_or_off():
    assert gre.down(_srv(via="gre"), T0, _host()) is None
    # туннель после включения ни разу не работал: это настройка, она видна в карточке
    assert gre.down(_srv(agent_at=None), T0, _host()) is None
    s = _srv()
    s.gre_enabled = False
    assert gre.down(s, T0, _host()) is None


def test_host_side_error_is_down_even_while_the_agent_still_uses_the_tunnel():
    err = "ufw did not take the rules for the tunnel"
    d = gre.down(_srv(via="gre"), T0, _host(ok=False, err=err))
    assert (d["level"], d["why"], d["direct"]) == (1, "host_error", False)
    assert d["text"] == f"GRE до панели: хост панели не поднял свою сторону туннеля (kvxg7): {err}, агент пока ходит туннелем"
    # и туннель еще не работал ни разу - хосту все равно чинить
    d = gre.down(_srv(agent_at=None), T0, _host(ok=False, err=err))
    assert (d["level"], d["direct"]) == (2, True)
    assert d["text"].startswith("GRE до панели не работает: хост панели не поднял")
    # ответ хоста давно устарел - его ошибка уже ничего не говорит
    assert gre.down(_srv(via="gre"), T0, _host(ok=False, err=err, ts=datetime.now(timezone.utc) - timedelta(hours=1))) is None


def test_tg_short_format_a():
    d = gre.down(_srv(connect_down=True), T0, _host())
    what, facts = srv_short.short("gre_down", {"items": d}, None)
    assert what == "GRE до панели не работает"
    assert facts == ["Сервер жив, агент ходит напрямую, отчеты могут теряться"]
    what, facts = srv_short.short("gre_down", {"items": gre.down(_srv(via="gre"), T0, _host(ok=False))}, None)
    assert (what, facts) == ("GRE до панели: ошибка на хосте панели", ["Агент пока ходит туннелем"])
    assert srv_short.icon("gre_down", 2) == "🔴🚇"


def _factory(path):
    from app.db import create_engine_and_factory

    return create_engine_and_factory(f"sqlite+aiosqlite:///{path.as_posix()}")


async def _env(tmp_path, monkeypatch, host=None):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "s.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=T0, cpu_alert_percent=0, mem_alert_percent=0, gre_enabled=True,
                     gre_net="10.254.0.0/30", gre_peer="203.0.113.10", gre_agent_at=T0, gre_seen_at=T0,
                     gre_state={"up": True}, last_report={"uptime_seconds": 100000}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr(gre, "host_state", lambda data_dir=None: host or {})
    # T0 - фиксированная дата: старт панели ставим до нее, иначе после T0 + 1 ч реального времени
    # panel_just_started(T0) истинно и оффлайн не считается
    monkeypatch.setattr(collector, "_PANEL_STARTED", T0 - timedelta(hours=1))
    return engine, factory, Settings(alert_webhook="http://hook"), sent


def _via(path: str):
    def rep(t):
        r = {"gre_path": {"via": path, "since": int(t.timestamp())}, "connect": "10.254.0.1"}
        if path == "direct":
            r["connect_down"] = True
        return r
    return rep


async def _tick(factory, cfg, t, rep, seen=True):
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        if seen:
            srv.last_seen = t
            srv.gre_seen_at = t
            srv.last_report = {"uptime_seconds": 100000, "clock_unix": t.timestamp(), **rep}
            if rep.get("gre_path", {}).get("via") == "gre":
                srv.gre_agent_at = t
        await s.commit()
    await collector.evaluate_servers(factory, cfg, t)


async def _replay(factory, cfg, plan, step_min=1, start=0):
    """plan: [(минута начала, отчет(t) или None - сервер молчит)]; тики раз в step_min с минуты start"""
    end, m = plan[-1][0], start
    while m <= end:
        rep = next(f for start, f in reversed(plan) if start <= m)
        t = T0 + timedelta(minutes=m)
        await _tick(factory, cfg, t, rep(t) if rep else {}, seen=rep is not None)
        m += step_min


async def _server(factory) -> Server:
    async with factory() as s:
        return (await s.execute(select(Server))).scalars().one()


def _gre_msgs(sent):
    return [x for x in sent if "GRE" in x]


async def test_short_fallbacks_are_quiet_and_a_steady_one_alerts_once(tmp_path, monkeypatch):
    """Агент дважды откатился напрямую на несколько минут - тихо. Потом напрямую 12 минут - одна
    тревога; вернулся в туннель - один отбой после выдержки нормы."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    plan = [(0, _via("gre")), (5, _via("direct")), (9, _via("gre")), (12, _via("direct")), (20, _via("gre")),
            (25, _via("direct")), (37, _via("direct"))]
    await _replay(factory, cfg, plan)
    msgs = _gre_msgs(sent)
    assert len(msgs) == 1, sent
    assert "GRE до панели не работает" in msgs[0] and "web-a" in msgs[0]
    srv = await _server(factory)
    assert (srv.alert_state or {}).get("gre_down") == 2
    # в "Что сломано" - то же самое, ведет в карточку GRE раздела Сеть
    pr = [p for p in collector.server_problems(srv, T0 + timedelta(minutes=37)) if p["kind"] == "gre_down"]
    assert pr and pr[0]["sec"] == "gre" and pr[0]["mute"] == "gre_down" and pr[0]["level"] == 2
    assert pr[0]["text"].endswith("туннель не ответил агенту")

    # снова в туннеле: отбой не сразу, а когда норма продержалась
    await _replay(factory, cfg, [(38, _via("gre")), (70, _via("gre"))], start=38)
    oks = [x for x in sent if x.startswith("✅")]
    assert len(oks) == 1 and "через GRE" in oks[0], sent
    assert len(_gre_msgs(sent)) == 2
    await engine.dispose()


async def test_whole_server_silent_is_offline_not_gre(tmp_path, monkeypatch):
    """Отчеты напрямую перестали доходить совсем (DPI режет и маленькие): сервер не на связи - про
    него алерт offline, про GRE не шлем."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    await _replay(factory, cfg, [(0, _via("gre")), (3, _via("direct")), (5, None), (30, None)])
    assert not _gre_msgs(sent), sent
    assert any("не на связи" in x.lower() or "📡" in x for x in sent), sent
    assert not (await _server(factory)).alert_state.get("gre_down")
    await engine.dispose()


async def test_gre_switched_off_closes_the_alert(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    await _replay(factory, cfg, [(0, _via("direct")), (12, _via("direct"))])
    assert len(_gre_msgs(sent)) == 1, sent
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.gre_enabled = False
        await s.commit()
    await _replay(factory, cfg, [(13, lambda t: {}), (40, lambda t: {})], start=13)
    assert (await _server(factory)).alert_state.get("gre_down") == 0
    assert [x for x in sent if x.startswith("✅")], sent
    await engine.dispose()


async def test_host_error_alerts_after_the_hold(tmp_path, monkeypatch):
    host = {"fw": "ufw", "ts": 2**31 - 1,
            "ifaces": {"kvxg1": {"peer": "203.0.113.10", "local": "198.51.100.5", "ok": False,
                                 "error": "ufw did not take the rules for the tunnel"}}}
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, host)
    await _replay(factory, cfg, [(0, _via("gre")), (8, _via("gre"))])
    assert not _gre_msgs(sent), sent
    await _replay(factory, cfg, [(9, _via("gre")), (12, _via("gre"))], start=9)
    msgs = _gre_msgs(sent)
    assert len(msgs) == 1 and "ошибка на хосте панели" in msgs[0], sent
    await engine.dispose()


async def test_switching_gre_on_again_waits_for_the_new_tunnel(client, auth_headers):
    """Выключили и включили GRE: отметка прежнего туннеля сбрасывается, пока новый не заработал -
    это настройка, а не "туннель лег"."""
    r = await client.post("/api/servers", json={"name": "web-b"}, headers=auth_headers)
    sid, tok = r.json()["server"]["id"], r.json()["token"]
    rep = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.56", "cpu_percent": 1.0, "mem_used": 1,
           "mem_total": 2, "disks": []}

    async def report(xff, **extra):
        r = await client.post("/api/agent/report", json={**rep, **extra},
                              headers={"Authorization": f"Bearer {tok}", "X-Forwarded-For": f"{xff}, 172.18.0.1"})
        assert r.status_code == 200, r.text

    async def switch(on):
        r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": on}, headers=auth_headers)
        assert r.status_code == 200, r.text
        return r.json()["gre"]

    await report("203.0.113.20")
    g = await switch(True)
    await report(g["node_addr"], connect=g["panel_addr"])
    g = await switch(True)  # повторное сохранение с той же галочкой ничего не сбрасывает
    assert g["agent_at"]
    await switch(False)
    g = await switch(True)
    assert g["agent_at"] is None
