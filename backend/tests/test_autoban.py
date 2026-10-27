"""Автобан сканеров (helper autoban-setup 0.1): блок отчета, где бан не работает, беды для алерта,
история банов, команды режима и разбана, белый список панели и сводка банов."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import alerts, autoban, collector, srv_short
from app.db import Base, create_engine_and_factory
from app.models import AutobanBan, BackupCommand, Location, Server

SRC = (Path(__file__).resolve().parents[2] / "agent" / "autoban-setup.sh").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
_NOT_KEYBOARD = re.compile("[\u2014\u2013\u00ab\u00bb\u2026\u00b7\u2265\u2264\u21b3\u2192\u0451\u0401\u00a0]")

BAN = {"ip": "203.0.113.10", "since": T - 600, "until": T - 600 + 86400, "n": 1, "hits": 41,
       "paths": ["/.env", "/.git/config", "/actuator/env"], "more": 12, "where": "api.example.com", "w": 0, "seen": 0}


@pytest.fixture(autouse=True)
def _fresh():
    autoban._last_ts.clear()


def _block(**kw) -> dict:
    b = {"v": 1, "ts": T, "mode": "on", "auto": "", "nft": True, "can": True, "err": "", "err_since": 0, "logs": 3,
         "cls": {"pub": 900, "cf": 0, "priv": 20}, "active": 1, "hour": 1, "day": 1, "drops": 7, "allow_sha": "",
         "bans": [BAN], "own": [], "proxy": [], "self": []}
    b.update(kw)
    return b


def _srv(block: dict | None, exposure=None, mutes=None, web=None, agent="2.55") -> Server:
    rep = {"extras": {"autoban": block} if block else {}, "web_services": web or [{"kind": "ingress-nginx"}]}
    return Server(id=1, name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep, alert_state={},
                  alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
                  disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
                  disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[],
                  external_ip="", local_ip="", exposure=exposure or {}, agent_version=agent)


# --- helper ---

def test_helper_header():
    assert "KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR" in SRC and "\r" not in SRC
    when = re.search(r'^KERVAX_SETUP_WHEN="([^"]*)"', SRC, re.M).group(1)
    assert not set(when) & set("{}';")
    assert 'chmod 0644 "$STATE_DIR/versions/autoban-setup.ver"' in SRC
    # бан только веб-портов и раньше DNAT docker и kube-proxy
    assert "type filter hook prerouting priority -300" in SRC
    assert "tcp dport { 80, 443 } counter drop" in SRC and "udp dport 443 counter drop" in SRC
    # свежая нода стартует в наблюдении, повторная установка режим не трогает
    assert "[ -f /etc/kervax/autoban.conf ] || printf 'mode=watch\\n'" in SRC
    # ловушки без security.txt и graphql
    parse = SRC[SRC.index("PARSE='"):SRC.index("# Hits within the window")]
    assert "\\/\\.well-known\\/" in parse and "graphql" not in parse
    # сроки: сутки, неделя, месяц; порог 3 разных ловушки за 10 минут
    assert "return n <= 1 ? 86400 : n == 2 ? 604800 : 2592000" in SRC
    assert "WINDOW=600" in SRC and "TRAPS=3" in SRC
    # Cloudflare и свои сети вшиты
    assert "172.64.0.0/13" in SRC and "100.64.0.0/10" in SRC and "2606:4700::/32" in SRC


def test_allow_sha_matches_helper():
    """Отпечаток списка у панели и у helper'а одинаковый (helper: sort | sha256sum | cut -c1-16)"""
    items = [("2001:db8::/48", "меш"), ("192.0.2.50/32", "Мой VPN")]
    want = hashlib.sha256(b"192.0.2.50/32\n2001:db8::/48\n").hexdigest()[:16]
    assert autoban.allow_sha(items) == want == "b543d6c9edc71488"  # значение из прогона helper'а в WSL
    assert autoban.allow_sha([]) == ""


# --- карточка: где бан не работает ---

def test_notes_say_where_ban_does_not_work():
    cf = {"cf": {"n": 7, "total": 7}}
    n = autoban.notes(_srv(_block(), cf), _block())
    assert n[0]["k"] == "cf" and n[0]["level"] == 2 and n[0]["text"].startswith("все сайты за Cloudflare")
    part = autoban.notes(_srv(_block(), {"cf": {"n": 3, "total": 7}}), _block())
    assert part[0]["level"] == 1 and part[0]["text"].startswith("3 из 7 сайтов за Cloudflare")
    caddy = _block(logs=0)
    t = autoban.notes(_srv(caddy, web=[{"kind": "Caddy"}]), caddy)
    assert [x["k"] for x in t] == ["nolog"] and t[0]["text"].startswith("край Caddy без access-лога")
    priv = _block(cls={"pub": 0, "cf": 0, "priv": 300})
    assert autoban.notes(_srv(priv, web=[{"kind": "Caddy"}, {"kind": "nginx"}]), priv)[0]["k"] == "priv"
    nonft = _block(nft=False)
    assert "nft" in [x["k"] for x in autoban.notes(_srv(nonft), nonft)]
    assert autoban.notes(_srv(_block()), _block()) == []


def test_view_splits_bans_and_would():
    b = _block(bans=[BAN, {**BAN, "ip": "198.51.100.4", "w": 1, "since": T - 60}],
               own=[{"ip": "192.0.2.50", "label": "Мой VPN", "hits": 154, "paths": ["/.env"], "more": 10, "last": T}])
    v = autoban.view(_srv(b), NOW)
    assert [x["ip"] for x in v["bans"]] == ["203.0.113.10"] and [x["ip"] for x in v["would"]] == ["198.51.100.4"]
    assert v["bans"][0]["why"] == "/.env, /.git/config, /actuator/env +12 - api.example.com, 41 удар"
    assert v["own"][0]["label"] == "Мой VPN" and v["agent_ok"] is True
    # блок протух - helper не работает, блока нет
    assert autoban.view(_srv(_block(ts=T - 3600)), NOW) is None


# --- алерт ---

def test_problems_and_alert():
    assert autoban.problems(_srv(_block()), NOW) == []
    bad = _block(self=[{"ip": "192.0.2.50", "label": "Мой VPN", "ts": T}])
    p = autoban.problems(_srv(bad), NOW)
    assert [x["key"] for x in p] == ["self:192.0.2.50"]
    assert p[0]["text"] == "автобан забанил свой адрес 192.0.2.50 (белый список: Мой VPN), бан снят"
    # не может банить: только после 10 минут подряд
    assert autoban.problems(_srv(_block(can=False, err="nft not found", err_since=T - 60)), NOW) == []
    cant = autoban.problems(_srv(_block(can=False, err="nft not found", err_since=T - 700)), NOW)
    assert cant[0]["key"] == "cant" and cant[0]["text"].endswith(": nft not found")
    # в watch "не может" - не беда
    assert autoban.problems(_srv(_block(mode="watch", can=False, err_since=T - 700)), NOW) == []
    # забаненные снова приходят: бан не действует
    leak = _block(bans=[{**BAN, "ip": f"203.0.113.{i}", "seen": 3 if i < 3 else 0} for i in range(1, 5)])
    assert [x["key"] for x in autoban.problems(_srv(leak), NOW)] == ["noeffect"]
    # лавина
    assert [x["key"] for x in autoban.problems(_srv(_block(hour=31)), NOW)] == ["avalanche"]
    assert "сам перешел в наблюдение" in autoban.problems(_srv(_block(mode="watch", auto=f"avalanche:{T}")), NOW)[0]["text"]
    # заглушено по виду беды
    assert autoban.problems(_srv(_block(hour=31), mutes=["autoban:avalanche"]), NOW) == []

    s = _srv(bad)
    lvl, ctx = collector._server_conditions(s, NOW)["autoban"]
    assert lvl == 2 and ctx["sig"] == ["self:192.0.2.50"]
    probs = [x for x in collector.server_problems(s, NOW) if x.get("kind") == "autoban"]
    assert probs and probs[0]["text"].startswith("автобан забанил свой адрес")
    short = autoban.tg_short(ctx["items"])
    assert short == ("Автобан забанил свой адрес", ["192.0.2.50 (белый список: Мой VPN), бан снят"])
    assert srv_short.TOPIC["autoban"] == "🚫" and srv_short.BASE["autoban"] == 2


# --- сводка ---

def test_digest_due_and_text():
    tz = ZoneInfo("Europe/Moscow")
    morning = datetime(2026, 10, 27, 6, 30, tzinfo=timezone.utc)  # 9:30 МСК
    assert autoban.digest_due("day", None, morning, tz)
    assert not autoban.digest_due("day", morning, morning + timedelta(hours=5), tz)
    assert not autoban.digest_due("day", None, datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc), tz)  # 8:00 МСК
    assert autoban.digest_due("hour", morning, morning + timedelta(minutes=61), tz)
    assert not autoban.digest_due("off", None, morning, tz)
    s = _srv(None)
    rows = [AutobanBan(server_id=1, ip=f"203.0.113.{i}", since=NOW, until=NOW + timedelta(days=1 if i else 7),
                       n=2 if i == 0 else 1, hits=10, paths=["/.env", "/.git/config", "/x.sql"], more=4, where="x",
                       watch=False, seen=0, end="", digested=False) for i in range(5)]
    txt = autoban.digest_text([(s, rows)], "day", "https://panel.example.com", tz)
    lines = txt.split("\n")
    assert lines[0] == "🚫 <b>Автобан за сутки: 5 адресов на 1 сервере</b>"
    assert lines[1].startswith('<a href="https://panel.example.com/?server=1&amp;sec=web">k8s-a</a> - 5: 203.0.113.0 /.env, /.git/config +5 до 03.11 15:00 повтор;')
    assert lines[1].endswith("; и еще 2")
    assert not _NOT_KEYBOARD.search(re.sub(r"<[^>]+>", "", txt))


async def _factory(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'a.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_store_history_and_digest(tmp_path, monkeypatch):
    engine, factory = await _factory(tmp_path)
    async with factory() as s:
        srv = _srv(_block())
        s.add(srv)
        await s.commit()
        await autoban.store(s, srv, {"autoban": _block()}, NOW)
        await s.commit()
        # тот же блок повторно (агент шлет его каждые 15 с) - не дублирует
        await autoban.store(s, srv, {"autoban": _block()}, NOW)
        await s.commit()
        rows = list(await s.scalars(select(AutobanBan)))
        assert len(rows) == 1 and rows[0].ip == "203.0.113.10" and rows[0].ended_at is None
        # следующий блок без бана: бан закрыт (снят раньше срока - gone)
        await autoban.store(s, srv, {"autoban": _block(ts=T + 60, bans=[])}, NOW + timedelta(minutes=1))
        await s.commit()
        h = await autoban.history(s, 1)
        assert h[0]["end"] == "gone" and h[0]["ended"] is not None

    sent = []

    async def fake_dispatch(cfg, enabled, msgs, threshold, **kw):
        sent.extend(msgs)
        return True

    async def cfg(session, settings):
        return {"telegram_token": "t", "telegram_chat": "c", "tz": "Europe/Moscow"}

    monkeypatch.setattr(alerts, "dispatch", fake_dispatch)
    monkeypatch.setattr(autoban.settings_store, "get_alert_config", cfg)
    monkeypatch.setattr(alerts, "alerts_enabled", lambda c: True)

    class St:
        panel_url = "https://panel.example.com"

    morning = datetime(2026, 10, 27, 6, 30, tzinfo=timezone.utc)
    assert await autoban.send_digest(factory, St(), morning) is True
    assert sent[0].kind == "autoban_digest" and "Автобан за сутки: 1 адрес на 1 сервере" in sent[0]
    # в тот же день второй раз не шлет, а бан помечен отправленным
    assert await autoban.send_digest(factory, St(), morning + timedelta(hours=3)) is False
    async with factory() as s:
        assert all(r.digested for r in await s.scalars(select(AutobanBan)))
    await engine.dispose()


async def test_sync_allow_pushes_list_once(tmp_path):
    engine, factory = await _factory(tmp_path)
    async with factory() as s:
        srv = _srv(_block())
        srv.external_ip = "198.51.100.20"
        old = _srv(_block(), agent="2.54")
        old.id, old.name = 2, "web-b"
        s.add_all([srv, old, Location(name="Алматы", url="socks5://x", exit_ip="192.0.2.77")])
        await autoban.save_settings(s, {"allow": [{"net": "192.0.2.50/32", "label": "Мой VPN"}], "digest": "day"})
        await s.commit()
    assert await autoban.sync_allow(factory, NOW) == 1  # у старого агента команды autoban нет
    assert await autoban.sync_allow(factory, NOW + timedelta(minutes=2)) == 0  # не чаще раза в 10 минут
    async with factory() as s:
        c = (await s.scalars(select(BackupCommand))).one()
        assert c.action == "autoban" and c.mode == "allow" and c.origin == "auto"
        assert c.paths == ["192.0.2.50/32 Мой VPN", "192.0.2.77/32 локация Алматы", "198.51.100.20/32 k8s-a"]
    await engine.dispose()


# --- API ---

async def test_api_view_unban_and_settings(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "k8s-a"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    now = int(datetime.now(timezone.utc).timestamp())
    blk = _block(ts=now, bans=[{**BAN, "since": now - 60, "until": now + 86000}])
    rep = {"hostname": "k8s-a", "os": "U", "agent_version": "2.54", "cpu_percent": 5, "mem_used": 1, "mem_total": 2,
           "extras": {"autoban": blk}}
    hdr = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/agent/report", json=rep, headers=hdr)).status_code == 200
    got = (await client.get(f"/api/servers/{sid}/autoban", headers=auth_headers)).json()
    assert got["view"]["bans"][0]["ip"] == "203.0.113.10" and got["history"][0]["ip"] == "203.0.113.10"
    # старый агент: кнопок нет
    r = await client.post(f"/api/servers/{sid}/autoban", json={"op": "unban", "ip": "203.0.113.10"}, headers=auth_headers)
    assert r.status_code == 400 and "2.55" in r.json()["detail"]
    rep["agent_version"] = "2.55"
    assert (await client.post("/api/agent/report", json=rep, headers=hdr)).status_code == 200
    r = await client.post(f"/api/servers/{sid}/autoban", json={"op": "unban", "ip": "bad"}, headers=auth_headers)
    assert r.status_code == 400
    r = await client.post(f"/api/servers/{sid}/autoban", json={"op": "ban", "ip": "203.0.113.10"}, headers=auth_headers)
    assert r.status_code == 422  # забанить из панели нельзя
    r = await client.post(f"/api/servers/{sid}/autoban", json={"op": "unban", "ip": "203.0.113.10"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["action"] == "autoban"
    r = await client.post(f"/api/servers/{sid}/autoban", json={"op": "on"}, headers=auth_headers)
    assert r.status_code == 200
    cmds = (await client.get("/api/agent/commands", headers=hdr)).json()
    bk = [c for c in cmds.get("backup_commands") or [] if c["action"] == "autoban"]
    assert [(c["mode"], c.get("name")) for c in bk] == [("unban", "203.0.113.10"), ("on", None)]

    r = await client.put("/api/autoban/settings", json={"allow": [{"net": "192.0.2.50", "label": "Мой VPN"},
                                                                  {"net": "192.0.2.50/32"}], "digest": "hour"},
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["allow"] == [{"net": "192.0.2.50/32", "label": "Мой VPN"}]
    assert r.json()["digest"] == "hour"
    r = await client.put("/api/autoban/settings", json={"allow": [{"net": "example.com"}]}, headers=auth_headers)
    assert r.status_code == 400
