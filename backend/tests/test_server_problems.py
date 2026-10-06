"""Сводка "Что сломано": collector.server_problems - короткие тексты новых проверок с теми же
уровнями, что у алертов."""
from datetime import datetime, timezone

from app.collector import server_problems
from app.models import Server
from tests.test_disk_health import _k8sd


def _server(rep: dict, now: datetime, **kw) -> Server:
    return Server(name="node", token_hash="x", enabled=True, last_seen=now, offline_after_seconds=120,
                  disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
                  last_report=rep, alert_mutes=kw.pop("alert_mutes", []), alert_snoozes={},
                  disk_forecast=kw.pop("disk_forecast", None), **kw)


def test_problems_from_all_new_checks():
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    rep = {
        "clock_unix": ts,
        "disks": [{"mount": "/", "used": 50, "total": 100, "inodes": 1000, "inodes_used": 920}],
        "extras": {
            "disk-health": _k8sd(ts - 30),
            "units": {"v": 1, "ts": ts - 30, "fix": True, "units": [
                {"unit": "certbot.service", "type": "oneshot", "result": "exit-code", "status": 1,
                 "since": ts - 3600, "log": []},
                {"unit": "fresh.service", "type": "simple", "result": "exit-code", "status": 1,
                 "since": ts - 10, "log": []},  # упал только что - ждем
                {"unit": "muted.service", "type": "simple", "result": "signal", "status": 9,
                 "since": ts - 3600, "log": []},
            ]},
        },
    }
    fc = {"ts": ts, "items": [
        {"mount": "/hdd", "kind": "space", "pct": 85.0, "rate": 3.0, "eta_h": 110.0},
        # дальше пяти суток, с которых приходит и алерт: в "Что сломано" не попадает
        {"mount": "/srv", "kind": "space", "pct": 78.0, "rate": 3.0, "eta_h": 176.0},
        {"mount": "/data", "kind": "space", "pct": 50.0, "rate": 1.0, "eta_h": 400.0}]}
    s = _server(rep, now, disk_forecast=fc, alert_mutes=["unit:muted.service"])
    probs = server_problems(s, now)
    texts = {(p["kind"], p["level"], p["text"]) for p in probs}
    assert ("disk_health", 3, "RAID md42, md43 (raid1): работает 1 из 2 дисков") in texts
    assert ("disk_health", 3, "диск nvme1n1 не определяется (размер 0)") in texts  # без модели
    assert ("disk_health", 2, "ошибки ввода-вывода: 40 за 10 минут") in texts
    assert ("units", 1, "certbot.service: код 1") in texts
    assert ("disk_forecast", 1, "/hdd заполнится через 5 дн") in texts
    assert ("inode", 2, "inode / 92%") in texts
    assert not any("fresh.service" in t or "muted.service" in t or "/data" in t or "/srv" in t
                   for _k, _l, t in texts)
    assert probs[0]["level"] == 3  # серьезные первыми
    by_kind = {p["kind"]: p for p in probs}
    assert by_kind["units"]["mute"] == "unit:certbot.service" and by_kind["units"]["sec"] == "units"
    assert by_kind["inode"]["mute"] == "inode@2"


def test_offline_server_has_no_problems():
    now = datetime.now(timezone.utc)
    s = _server({"extras": {"disk-health": _k8sd(int(now.timestamp()))}}, now)
    s.last_seen = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert server_problems(s, now) == []


async def test_problems_in_servers_list(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "p"}, headers=auth_headers)
    token = r.json()["token"]
    report = {"hostname": "h", "os": "U", "agent_version": "2.18", "cpu_percent": 5, "mem_used": 1,
              "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 100, "inodes": 100, "inodes_used": 97}]}
    assert (await client.post("/api/agent/report", json=report,
                              headers={"Authorization": f"Bearer {token}"})).status_code == 200
    srv = (await client.get("/api/servers", headers=auth_headers)).json()[0]
    assert srv["problems"] == [{"kind": "inode", "level": 3, "text": "inode / 97%", "sec": "diskfill", "mute": "inode",
                               "since": None}]
