"""Сроки кластера "не нужен, не следить" (app/kube_expiry.py).

Повод: на кластере четыре старых TLS-секрета истекли полгода назад, никто их не использует, а
раздел "Сроки" и главная из-за них навсегда красные. Удалять секреты панель не может и не
должна, поэтому пункт отмечают, и он уходит из проблем и алертов. Обновили секрет - снова следим,
удалили из кластера - отметка тихо уходит."""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import collector, kube_expiry
from app.models import Server

DAY = 86400


def _srv(now, items, ignored=None, **kw):
    base = dict(id=1, name="node-1", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                offline_after_seconds=120, alert_sustain_seconds=900,
                last_report={"clock_unix": now.timestamp(), "kube_expiry": items},
                alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[7, 1], kube_expiry_ignored=ignored)
    base.update(kw)
    return Server(**base)


def _probs(s, now):
    return [p for p in collector.server_problems(s, now) if p["kind"] == "kube_expiry"]


def test_ignored_item_leaves_problems_and_comes_back_when_renewed():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    ts = int(now.timestamp())
    old = {"kind": "secret-cert", "where": "default/old-ssl", "expires": ts - 200 * DAY, "note": "tls.crt",
           "fp": "aa11", "ingress": False}
    used = {"kind": "secret-cert", "where": "default/api-ssl", "expires": ts + 3 * DAY, "note": "tls.crt",
            "fp": "bb22", "ingress": True}
    s = _srv(now, [old, used])
    # не отмечен: истекший первым, с подсказкой, что его никто не использует
    got = _probs(s, now)
    assert [p["level"] for p in got] == [3]
    c = collector.kube_expiry_ctx(*collector.kube_expiry_soon(s.last_report, [7, 1], now, s))
    assert c["unused"] and "не используется ни одним ingress" in c["advice"]

    s.kube_expiry_ignored = {"secret-cert|default/old-ssl": kube_expiry.entry(old, "admin", now)}
    # отмеченный ушел, остался тот, за которым следим
    got = _probs(s, now)
    assert [p["level"] for p in got] == [1] and "default/api-ssl" in got[0]["text"]
    assert kube_expiry.view(s) == {"secret-cert|default/old-ssl": {"by": "admin", "at": now.isoformat()}}

    # секрет перевыпустили: другой срок - отметка не действует, за ним снова следим
    renewed = dict(old, expires=ts + 2 * DAY, fp="cc33")
    s.last_report = {"clock_unix": ts, "kube_expiry": [renewed, used]}
    assert "default/old-ssl" in _probs(s, now)[0]["text"]
    assert kube_expiry.view(s) is None
    assert kube_expiry.step(s.kube_expiry_ignored, s.last_report, now) == {"secret-cert|default/old-ssl": None}
    # тот же срок, но другой отпечаток - тоже новый сертификат
    assert kube_expiry.active(s, {"kube_expiry": [dict(old, fp="dd44")]}) == {}
    # отпечатка не знали (старый helper) - сверяем только срок
    assert kube_expiry.active(s, {"kube_expiry": [dict(old, fp="")]}) != {}


def test_deleted_secret_mark_goes_away_quietly():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    it = {"kind": "secret-cert", "where": "default/old-ssl", "expires": 1, "fp": "aa"}
    other = {"kind": "kubeconfig", "where": "/root/.kube/config", "expires": 2}
    ign = {"secret-cert|default/old-ssl": kube_expiry.entry(it, "admin", now)}
    rep = {"kube_expiry": [other]}
    # пропал из отчета - сначала только помечаем: helper мог разово не дочитать секреты
    ch = kube_expiry.step(ign, rep, now)
    assert ch["secret-cert|default/old-ssl"]["gone_at"] == now.isoformat()
    ign = {k: v for k, v in ch.items() if v}
    assert kube_expiry.step(ign, rep, now + timedelta(hours=2)) == {}
    # вернулся - отметка снова целая
    back = kube_expiry.step(ign, {"kube_expiry": [it, other]}, now + timedelta(hours=3))
    assert "gone_at" not in back["secret-cert|default/old-ssl"]
    # нет сутки - снимаем
    assert kube_expiry.step(ign, rep, now + timedelta(days=1, minutes=1)) == {"secret-cert|default/old-ssl": None}
    # пустой список сроков - скорее сбой helper'а: ничего не трогаем
    assert kube_expiry.step(ign, {"kube_expiry": []}, now + timedelta(days=5)) == {}


async def test_collector_drops_mark_of_renewed_secret(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'ke.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    old = {"kind": "secret-cert", "where": "default/old-ssl", "expires": ts - 100 * DAY, "fp": "aa"}
    keep = {"kind": "secret-cert", "where": "default/legacy-ssl", "expires": ts - 50 * DAY, "fp": "bb"}
    async with factory() as s:
        s.add(Server(name="node-1", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, created_at=now - timedelta(days=30),
                     last_report={"clock_unix": ts, "uptime_seconds": 100000, "kube_expiry": [old, keep]},
                     kube_expiry_ignored={"secret-cert|default/old-ssl": kube_expiry.entry(old, "admin", now),
                                          "secret-cert|default/legacy-ssl": kube_expiry.entry(keep, "admin", now)}))
        await s.commit()

    async def fake_send(cfg, text, parse_mode=None):
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = {"clock_unix": ts, "uptime_seconds": 100000,
                           "kube_expiry": [dict(old, expires=ts + 80 * DAY, fp="cc"), keep]}
        await s.commit()
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
    assert list(srv.kube_expiry_ignored) == ["secret-cert|default/legacy-ssl"]
    await engine.dispose()


async def test_ignore_api(client: httpx.AsyncClient, auth_headers):
    r = await client.post("/api/servers", json={"name": "node-1"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ts = int(datetime.now(timezone.utc).timestamp())
    report = {
        "hostname": "node-1", "os": "Ubuntu 24.04", "agent_version": "2.49",
        "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2, "load": [0.1, 0.1, 0.1],
        "disks": [{"mount": "/", "used": 1, "total": 2}],
        "kube_expiry": [{"kind": "secret-cert", "where": "default/old-ssl", "expires": ts - 100 * DAY,
                         "note": "tls.crt", "fp": "aa", "ingress": False}],
    }
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    body = {"kind": "secret-cert", "where": "default/old-ssl", "ignored": True}
    r = await client.post(f"/api/servers/{sid}/kube/expiry-ignore", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    got = r.json()["kube_expiry_ignored"]["secret-cert|default/old-ssl"]
    assert got["by"] and got["at"] and set(got) == {"by", "at"}
    # пункт как его прислал helper: отпечаток и ingress доехали через схему отчета
    assert r.json()["last_report"]["kube_expiry"][0]["ingress"] is False

    r = await client.post(f"/api/servers/{sid}/kube/expiry-ignore", headers=auth_headers, json=dict(body, ignored=False))
    assert r.json()["kube_expiry_ignored"] is None
    # такого пункта нет в отчете
    r = await client.post(f"/api/servers/{sid}/kube/expiry-ignore", headers=auth_headers,
                          json=dict(body, where="default/nope"))
    assert r.status_code == 404


def test_helper_marks_ingress_use_in_a_shell():
    """Сценарий tests/sh/kube_expiry_ingress.sh: настоящий helper с поддельным kubectl - отпечаток
    сертификата и используется ли секрет в Ingress. Нужен Linux с openssl, иначе пропуск; на
    Windows его гоняют в WSL: wsl -- bash tests/sh/kube_expiry_ingress.sh <корень репо>."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    if sys.platform != "linux" or not shutil.which("openssl") or not shutil.which("python3"):
        pytest.skip("нужен Linux с openssl")
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(["bash", str(Path(__file__).parent / "sh" / "kube_expiry_ingress.sh"), str(root)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and r.stdout.strip().endswith("PASS"), r.stdout + r.stderr


def test_helper_and_agent_carry_the_new_fields():
    from pathlib import Path

    agent = Path(__file__).resolve().parents[2] / "agent"
    sh = (agent / "kubeexpiry-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.3" in sh and "get ingresses -A -o json" in sh
    # агент читает файл helper'а в структуру: без этих полей он бы их молча выбросил
    main = (agent / "main.go").read_text(encoding="utf-8")
    assert '`json:"fp,omitempty"`' in main and '`json:"ingress,omitempty"`' in main
