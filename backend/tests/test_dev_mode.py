"""Сервер разработки (app/dev_mode.py): поведение приложений не будит алертами, инфраструктура - как у
всех."""
from datetime import datetime, timedelta, timezone

from app import collector, dev_mode, mute_watch, settings_store
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server


def test_every_kind_in_one_group():
    """Каждый вид серверных алертов и "Что сломано" отнесен ровно к одной группе: новый вид без
    решения, будит ли он на разработке, тест не пропустит."""
    kinds = set(settings_store.SERVER_ALERT_KINDS) | set(mute_watch.PROBLEM_KINDS)
    assert not dev_mode.APP & dev_mode.INFRA
    assert kinds - (dev_mode.APP | dev_mode.INFRA) == set()
    assert (dev_mode.APP | dev_mode.INFRA) - kinds == set()  # без устаревших


def test_split_by_user_words():
    # что разработчики ломают сами - тихо
    for k in ("kube_restarts", "kube_pod", "kube_job", "docker_loop", "workers", "db_health", "web_5xx",
              "log_errors", "units"):
        assert dev_mode.quiet(k), k
    # машина, бэкапы, безопасность, сертификаты - будят
    for k in ("offline", "disk", "inode", "mem", "oom", "disk_health", "backup_failed", "ssh_risk",
              "access_change", "port_new", "web_cert", "docker_risk"):
        assert not dev_mode.quiet(k), k


def test_looks_dev():
    for n in ("app-dev", "de-x-api-dev", "web-stage", "api-staging-2", "ci-test", "dev-db", "web_dev1"):
        assert dev_mode.looks_dev(n), n
    for n in ("web-prod", "devops-1", "latest-box", "contest", "stagecoach", "", "gitlab"):
        assert not dev_mode.looks_dev(n), n


def test_mark():
    probs = [{"kind": "kube_restarts", "level": 2}, {"kind": "disk", "level": 3}]
    dev_mode.mark(probs)
    assert probs[0].get("dev") is True and "dev" not in probs[1]


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


def _rep(t: datetime, unit_failed: bool, disk: int) -> dict:
    ts = int(t.timestamp())
    u = [{"unit": "app-worker.service", "type": "simple", "result": "exit-code", "status": 1,
          "since": ts - 600, "log": ["app-worker.service: Main process exited, code=exited, status=1/FAILURE"]}]
    return {"disks": [{"mount": "/", "used": disk, "total": 100}], "clock_unix": ts,
            "extras": {"units": {"v": 1, "ts": ts - 30, "units": u if unit_failed else [], "fix": True}}}


def _srv(name: str, now: datetime, **kw) -> Server:
    base = dict(
        name=name, token_hash=name, enabled=True, backup_not_required=True, last_seen=now,
        cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=85, disk_alert_percent=90,
        disk_crit_percent=95, alert_sustain_seconds=0, offline_after_seconds=120,
        created_at=now - timedelta(days=10), first_report_at=now - timedelta(days=10),
    )
    base.update(kw)
    return Server(**base)


async def test_dev_server_sends_only_infra(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        # у обоих упал юнит приложения, у обоих диск 97%
        s.add(_srv("app-dev", now, is_dev=True, last_report=_rep(now, True, 97)))
        s.add(_srv("app-prod", now, last_report=_rep(now, True, 97)))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    dev = [m for m in sent if "app-dev" in m]
    prod = [m for m in sent if "app-prod" in m]
    assert any("app-worker" in m for m in prod), sent
    assert dev and all("app-worker" not in m for m in dev), sent  # диск ушел, юнит нет
    assert any("97%" in m for m in dev), sent
    async with factory() as s:
        a = await s.get(Server, 1)
        assert a.alert_state.get("units")  # отметка записана, как будто ушло
    # юнит поднялся: отбоя про то, о чем не говорили, тоже нет
    sent.clear()
    later = now + timedelta(minutes=30)
    async with factory() as s:
        for sid, name in ((1, "app-dev"), (2, "app-prod")):
            row = await s.get(Server, sid)
            row.last_seen, row.last_report = later, _rep(later, False, 97)
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    await collector.evaluate_servers(factory, settings, later + timedelta(minutes=30))
    assert not [m for m in sent if "app-dev" in m and "app-worker" in m], sent
    await engine.dispose()


async def test_toggle_in_settings(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "web-stage"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    srv = r.json()["server"]
    sid = srv["id"]
    assert srv["is_dev"] is False and srv["dev_hint"] is True  # подсказка, а не пометка
    r = await client.patch(f"/api/servers/{sid}", json={"is_dev": True}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["is_dev"] is True and r.json()["dev_hint"] is False
    r = await client.patch(f"/api/servers/{sid}", json={"is_dev": None}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["is_dev"] is True
    r = await client.patch(f"/api/servers/{sid}", json={"is_dev": False}, headers=auth_headers)
    assert r.json()["is_dev"] is False
    r = await client.post("/api/servers", json={"name": "web-9", "group_name": "Dev"}, headers=auth_headers)
    assert r.json()["server"]["dev_hint"] is True  # и по группе
    r = await client.post("/api/servers", json={"name": "web-10"}, headers=auth_headers)
    assert r.json()["server"]["dev_hint"] is False
    r = await client.get("/api/audit", headers=auth_headers)
    acts = [a["action"] for a in r.json() if a["target"] == "web-stage"]
    assert "server_dev_on" in acts and "server_dev_off" in acts
