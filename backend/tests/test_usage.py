"""Кто качает трафик (агент 2.34, net_users) и кто пишет на диск (2.33, disk_writers) - по часам
за неделю: панель раскладывает топы из отчета по часовым корзинам."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app import usage
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerUsage

T = datetime(2026, 10, 17, 12, 5, tzinfo=timezone.utc)
H12 = int(datetime(2026, 10, 17, 12, tzinfo=timezone.utc).timestamp() // 3600)
REP = {"agent_version": "2.34", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}]}


def test_spread_splits_hours_and_caps_window():
    app = {"kind": "container", "name": "shop-app", "rx": 3600, "tx": 7200, "s": 3600}
    # 10 минут через границу часа: по пять в каждый час, скорость из окна агента
    got = usage.spread([app], ("rx", "tx"), T - timedelta(minutes=10), T)
    assert got == {(H12 - 1, "shop-app"): [300.0, 600.0, "container"], (H12, "shop-app"): [300.0, 600.0, "container"]}
    # перерыв дольше окна: в корзину только окно, раньше агент не помнит
    short = dict(app, s=600, rx=600, tx=0)
    got = usage.spread([short], ("rx", "tx"), T - timedelta(hours=2), T)
    assert got == {(H12 - 1, "shop-app"): [300.0, 0.0, "container"], (H12, "shop-app"): [300.0, 0.0, "container"]}
    # пустые, без окна и мусор - мимо
    assert usage.spread([{"name": "x", "rx": 0, "tx": 0, "s": 60}, {"name": "y", "rx": 5, "s": 0}, "z", None,
                         {"name": "", "rx": 5, "s": 60}, {"name": "w", "rx": "?", "s": 60}],
                        ("rx", "tx"), T - timedelta(minutes=10), T) == {}


async def test_ingest_and_view(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    net = [{"kind": "container", "name": "shop-app", "rx": 3600, "tx": 36000, "s": 3600},
           {"kind": "pod", "name": "shop/web-0", "rx": 360, "tx": 0, "s": 3600}]
    disk = [{"kind": "unit", "name": "postgresql.service", "w": 7200, "r": 0, "s": 3600}]
    async with factory() as s:
        srv = Server(id=1, name="web", token_hash="x")
        s.add(srv)
        await s.flush()
        # первый раз - все окно агента (час), из них 5 минут в текущий час
        await usage.ingest(s, srv, net, disk, T)
        # чаще STEP не складываем
        await usage.ingest(s, srv, net, disk, T + timedelta(minutes=5))
        await s.commit()
        rows = {(r.metric, r.hour, r.name): (r.a, r.b, r.kind) for r in (await s.execute(select(ServerUsage))).scalars()}
        assert rows[("net", H12, "shop-app")] == (300, 3000, "container")
        assert rows[("net", H12 - 1, "shop-app")] == (3300, 33000, "container")
        assert rows[("disk", H12, "postgresql.service")] == (600, 0, "unit")
        # через 10 минут - еще 10 минут по скорости из отчета
        await usage.ingest(s, srv, net, disk, T + timedelta(minutes=10))
        await s.commit()
        r = await s.get(ServerUsage, (1, "net", H12, "shop-app"))
        assert (r.a, r.b) == (900, 9000)
        # прошлые сутки по местному времени (Москва, UTC+3): 23:30 UTC - уже следующий день
        s.add(ServerUsage(server_id=1, metric="net", hour=H12 - 12, name="shop-app", kind="container", a=1, b=1))
        s.add(ServerUsage(server_id=1, metric="net", hour=H12 - 30, name="old-job", kind="container", a=0, b=10**9))
        s.add(ServerUsage(server_id=1, metric="net", hour=H12 - 24 * 9, name="gone", kind="container", a=5, b=5))
        await s.commit()
        v = await usage.view(s, 1, "net", T + timedelta(minutes=10), tz_min=-180)
        assert v["days"][-1] == "2026-10-17" and len(v["days"]) == 7
        names = [x["name"] for x in v["names"]]
        assert names == ["old-job", "shop-app", "shop/web-0"]
        app = v["names"][1]
        # последние сутки: прошлый час, текущий и сегодняшнее утро; вчерашний old-job - только в неделе
        assert app["d24"] == [3300 + 900 + 1, 33000 + 9000 + 1] and app["week"] == app["d24"]
        assert app["days"][-1] == [4201, 42001] and app["days"][-2] == [0, 0]
        old = v["names"][0]
        assert old["d24"] == [0, 0] and old["days"][-2] == [0, 10**9]
        d = await usage.view(s, 1, "disk", T + timedelta(minutes=10))
        assert [x["name"] for x in d["names"]] == ["postgresql.service"] and d["names"][0]["kind"] == "unit"
    await engine.dispose()


async def test_report_lands_in_usage(client, auth_headers):
    r = (await client.post("/api/servers", json={"name": "web"}, headers=auth_headers)).json()
    sid, ah = r["server"]["id"], {"Authorization": f"Bearer {r['token']}"}
    rep = dict(REP, net_users=[{"kind": "container", "name": "shop-app", "rx": 1000, "tx": 9000, "s": 3600}],
               disk_writers=[{"kind": "container", "name": "shop-db", "w": 5000, "r": 10, "s": 600}])
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    got = (await client.get(f"/api/servers/{sid}/usage?metric=net&tz=-180", headers=auth_headers)).json()
    app = got["names"][0]
    assert app["name"] == "shop-app" and app["d24"] == [1000, 9000] and got["since"]
    got = (await client.get(f"/api/servers/{sid}/usage?metric=disk", headers=auth_headers)).json()
    assert got["names"][0]["week"] == [5000, 10]
    assert (await client.get(f"/api/servers/{sid}/usage?metric=cpu", headers=auth_headers)).status_code == 422
    # сервер удален - его история тоже
    assert (await client.delete(f"/api/servers/{sid}", headers=auth_headers)).status_code in (200, 204)


def test_agent_collects_net_users():
    src = (Path(__file__).resolve().parents[2] / "agent" / "main.go").read_text(encoding="utf-8")
    # пространство по inode net/dev: ссылку ns/net не от root не прочитать
    assert 'fileIno(proc + "/self/net/dev")' in src and "ino == host" in src
    assert 'r.NetUsers = netUsers("/sys/fs/cgroup", "/proc", cn, pn, time.Now())' in src
