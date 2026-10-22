"""Куда идет бэкап ноды: адрес репозитория без логина и пароля, канал и пункт "бэкап по http"."""
import time

from app import backup_target


def test_clean_strips_credentials():
    assert backup_target.clean("rest:http://node-a:fakepass@203.0.113.5:64100/node-a/") == \
        "rest:http://203.0.113.5:64100/node-a/"
    assert backup_target.clean("rest:https://u:p@backup.example.com/u/") == "rest:https://backup.example.com/u/"
    assert backup_target.clean("sftp:user@host-b:/srv/restic") == "sftp:host-b:/srv/restic"
    assert backup_target.clean("s3:https://key:secret@s3.example.com/bucket") == "s3:https://s3.example.com/bucket"
    assert backup_target.clean("rest:http://203.0.113.5:64100/node-a/") == "rest:http://203.0.113.5:64100/node-a/"
    assert backup_target.clean(None) == ""

    rep = {"backup": {"repo_dest": "rest:http://node-a:fakepass@203.0.113.5:64100/node-a/"}}
    backup_target.clean_report(rep)
    assert "fakepass" not in str(rep) and "node-a:" not in rep["backup"]["repo_dest"]
    backup_target.clean_report({})  # без блока бэкапа - ничего


def test_parse_schemes():
    p = backup_target.parse
    assert p("rest:http://203.0.113.5:64100/node-a/") == {
        "kind": "rest", "proto": "http", "host": "203.0.113.5", "port": 64100, "repo": "node-a"}
    assert p("rest:https://backup.example.com/node-a/") == {
        "kind": "rest", "proto": "https", "host": "backup.example.com", "port": 443, "repo": "node-a"}
    assert p("rest:http://[2001:db8::5]:64100/node-a")["host"] == "2001:db8::5"
    assert p("sftp:host-b:/srv/restic")["proto"] == "sftp"
    assert p("s3:s3.example.com/bucket/x") == {
        "kind": "s3", "proto": "https", "host": "s3.example.com", "port": 443, "repo": "bucket/x"}
    assert p("/mnt/backup/restic")["kind"] == "local"
    # логин и пароль не доходят до разобранного адреса, даже если helper их прислал
    assert "fakepass" not in str(p("rest:http://node-a:fakepass@203.0.113.5:64100/node-a/"))
    assert p("") is None and p("garbage") is None


def test_view_flags_http_only_through_the_internet():
    def v(dest, mutes=None):
        return backup_target.view({"backup": {"repo_dest": dest}}, mutes)

    assert v("rest:http://203.0.113.5:64100/node-a/")["insecure"] is True
    assert v("rest:http://backup.example.com:64100/node-a/")["insecure"] is True  # имя: где оно, не знаем
    assert v("rest:https://203.0.113.5:64101/node-a/")["insecure"] is False
    # внутренние адреса: частная сеть, меш, сам сервер бэкапов
    for host in ("10.0.0.5", "192.168.1.5", "100.100.0.7", "127.0.0.1", "localhost"):
        assert v(f"rest:http://{host}:64100/node-a/")["insecure"] is False, host
    assert v("rest:http://203.0.113.5:64100/node-a/", [backup_target.MUTE_KEY])["muted"] is True
    assert backup_target.view({}) is None
    assert backup_target.view({"backup": {"present": True}}) is None


async def test_report_stores_destination_without_credentials(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "node-a"}, headers=auth_headers)
    sid, token = r.json()["server"]["id"], r.json()["token"]
    rep = {"hostname": "node-a", "os": "Debian", "agent_version": "2.47", "cpu_percent": 5,
           "mem_used": 1, "mem_total": 2, "clock_unix": int(time.time()),
           "backup": {"present": True, "repo_dest": "rest:http://node-a:fakepass@203.0.113.5:64100/node-a/"}}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert "fakepass" not in str(srv)
    assert srv["last_report"]["backup"]["repo_dest"] == "rest:http://203.0.113.5:64100/node-a/"
    assert srv["backup_target"] == {"kind": "rest", "proto": "http", "host": "203.0.113.5", "port": 64100,
                                    "repo": "node-a", "insecure": True, "muted": False}
    # решение "оставить http" - в alert_mutes, пункт уходит из "Требует действий"
    r = await client.patch(f"/api/servers/{sid}", json={"alert_mutes": [backup_target.MUTE_KEY]}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["backup_target"]["muted"] is True


# ------------------------------- перевод на https -------------------------------

def _app(client):
    return client._transport.app


async def _pair(client, auth_headers, n, dest="rest:http://203.0.113.{n}:64100/node-{n}/", agent="2.48",
                helper="0.40", bs_helper="0.35", tls_front=True):
    from app.models import Server

    a = (await client.post("/api/servers", json={"name": f"node-{n}"}, headers=auth_headers)).json()["server"]["id"]
    b = (await client.post("/api/servers", json={"name": f"bk-{n}"}, headers=auth_headers)).json()["server"]["id"]
    async with _app(client).state.session_factory() as s:
        ca, cb = await s.get(Server, a), await s.get(Server, b)
        ca.last_report = {"backup": {"present": True, "repo_dest": dest.format(n=n)},
                          "setup_versions": {"backup-setup": helper}}
        ca.agent_version, ca.agent_ip = agent, "198.51.100.5"
        cb.last_report = {"backup_server": {"present": True, "running": True, "port": 64100, "tls_port": 64101,
                                            "tls_front": tls_front},
                          "setup_versions": {"backupserver-setup": bs_helper}}
        cb.agent_version, cb.external_ip = "2.48", f"203.0.113.{n}"
        await s.commit()
    return a, b


async def test_to_https_needs_new_agent_and_helpers(client, auth_headers, monkeypatch):
    from app.api import servers as srv_api

    started = []

    async def fake(_f, job_id, params):
        started.append(params)

    monkeypatch.setattr(srv_api, "_orchestrate_to_https", fake)
    a, b = await _pair(client, auth_headers, 1, agent="2.47", helper="0.39", bs_helper="0.34")
    lst = {x["id"]: x for x in (await client.get("/api/servers", headers=auth_headers)).json()}
    why = lst[a]["backup_https"]["blockers"]
    assert lst[a]["backup_https"]["bs_id"] == b
    assert "node-1: нужен агент 2.48 или новее" in why
    assert "node-1: нужен helper backup-setup 0.40 или новее" in why
    assert "bk-1: нужен helper backupserver-setup 0.35 или новее" in why
    r = await client.post(f"/api/servers/{a}/backup/to-https", headers=auth_headers)
    assert r.status_code == 400 and "нужен агент 2.48" in r.json()["detail"]
    assert started == []

    # все готово: задача, сертификат на тот адрес, по которому клиент уже ходит
    a, b = await _pair(client, auth_headers, 2, tls_front=False)
    lst = {x["id"]: x for x in (await client.get("/api/servers", headers=auth_headers)).json()}
    assert lst[a]["backup_https"] == {"bs_id": b, "blockers": []}
    r = await client.post(f"/api/servers/{a}/backup/to-https", headers=auth_headers)
    assert r.status_code == 200, r.text
    p = started[-1]
    assert (p["client_id"], p["bs_id"], p["tls_front"], p["tls_port"], p["san_ip"], p["client_ip"]) == \
        (a, b, False, 64101, "203.0.113.2", "198.51.100.5")

    # уже https и сервер бэкапов не из панели - перевести нечего или некуда
    a, _ = await _pair(client, auth_headers, 3, dest="rest:https://203.0.113.{n}:64101/node-{n}/")
    lst = {x["id"]: x for x in (await client.get("/api/servers", headers=auth_headers)).json()}
    assert lst[a]["backup_https"] is None
    a, _ = await _pair(client, auth_headers, 4, dest="rest:http://192.0.2.200:64100/node-{n}/")
    r = await client.post(f"/api/servers/{a}/backup/to-https", headers=auth_headers)
    assert r.status_code == 400 and "192.0.2.200 не найден в панели" in r.json()["detail"]


async def test_bulk_to_https_goes_one_by_one(client, auth_headers, monkeypatch):
    from app.api import servers as srv_api
    from app.models import Server

    order = []

    async def fake(_f, job_id, params):
        order.append((params["client_id"], params["tls_front"]))

    monkeypatch.setattr(srv_api, "_orchestrate_to_https", fake)
    a1, b = await _pair(client, auth_headers, 11, tls_front=False)
    a2 = (await client.post("/api/servers", json={"name": "node-12"}, headers=auth_headers)).json()["server"]["id"]
    async with _app(client).state.session_factory() as s:
        c2 = await s.get(Server, a2)
        c2.last_report = {"backup": {"present": True, "repo_dest": "rest:http://203.0.113.11:64100/node-12/"},
                          "setup_versions": {"backup-setup": "0.40"}}
        c2.agent_version, c2.agent_ip = "2.48", "198.51.100.6"
        await s.commit()
    r = await client.post(f"/api/servers/{b}/backup-server/to-https", json={"client_ids": [a1, a2]},
                          headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [x["server_id"] for x in r.json()] == [a1, a2] and all(x["status"] == "running" for x in r.json())
    import asyncio
    for _ in range(50):
        if len(order) == 2:
            break
        await asyncio.sleep(0.02)
    # по очереди, TLS rest-server поднимает только первый
    assert order == [(a1, False), (a2, True)]

    # клиент чужого сервера бэкапов - отказ всей пачке до начала
    other, _ = await _pair(client, auth_headers, 13)
    r = await client.post(f"/api/servers/{b}/backup-server/to-https", json={"client_ids": [a1, other]},
                          headers=auth_headers)
    assert r.status_code == 400 and "бэкап идет не на" in r.json()["detail"]


async def test_to_https_orchestration_steps(client, auth_headers, monkeypatch):
    from app.api import servers as srv_api
    from app.models import BackupSetupJob

    a, b = await _pair(client, auth_headers, 21, tls_front=False)
    sent = []
    answers = {"deploy_tls_front": (True, "OK"), "get_cert": (True, "Q0VSVA=="), "allow_tls": (True, "OK"),
               "to_https": (False, "the check over https failed, the backup stays on http: x509")}

    async def create(_f, sid, action, payload):
        sent.append((sid, action, payload))
        return len(sent)

    async def wait(_f, cid, _timeout):
        return answers[sent[cid - 1][1]]

    monkeypatch.setattr(srv_api, "_bcmd_create", create)
    monkeypatch.setattr(srv_api, "_bcmd_wait", wait)
    factory = _app(client).state.session_factory
    async with factory() as s:
        job = BackupSetupJob(server_id=a, backup_server_id=b, status="running", steps=[])
        s.add(job)
        await s.commit()
        jid = job.id
    params = {"client_id": a, "client_ip": "198.51.100.5", "bs_id": b, "bs_hostname": "bk-21", "tls_front": False,
              "tls_port": 64101, "san_ip": "203.0.113.16", "_user": ""}
    await srv_api._orchestrate_to_https(factory, jid, params)
    assert [x[1] for x in sent] == ["deploy_tls_front", "get_cert", "allow_tls", "to_https"]
    assert sent[0][2] == {"san_ip": "203.0.113.16", "san_dns": "bk-21"}
    assert sent[2] == (b, "allow_tls", {"client_ip": "198.51.100.5"})
    assert sent[3] == (a, "to_https", {"port": 64101, "cacert_b64": "Q0VSVA=="})
    async with factory() as s:
        job = await s.get(BackupSetupJob, jid)
        assert job.status == "error" and "stays on http" in job.message


def test_helpers_know_the_new_actions():
    from pathlib import Path

    agent = Path(__file__).resolve().parents[2] / "agent"
    cl = (agent / "backup-setup.sh").read_text(encoding="utf-8")
    bs = (agent / "backupserver-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.40" in cl and 'to_https)     if out="$(cmd_to_https' in cl
    assert "KERVAX_SETUP_VERSION=0.35" in bs and 'allow_tls)        if out="$(cmd_allow_tls' in bs
    import re

    main = (agent / "main.go").read_text(encoding="utf-8")
    ver = re.search(r'^const version = "(\d+)\.(\d+)"$', main, re.M)
    assert ver and (int(ver[1]), int(ver[2])) >= tuple(backup_target.AGENT_MIN)


def test_to_https_scenario_in_a_shell():
    """Сценарий tests/sh/backup_to_https.sh: настоящий helper клиента с поддельными restic и
    systemctl. Нужен Linux и root, иначе пропуск; на Windows его гоняют в WSL:
    wsl -u root -- bash tests/sh/backup_to_https.sh <корень репо>."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    if sys.platform != "linux" or os.geteuid() != 0 or not shutil.which("base64"):
        pytest.skip("нужен Linux и root")
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run(["bash", str(Path(__file__).parent / "sh" / "backup_to_https.sh"), str(root)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and r.stdout.strip().endswith("PASS"), r.stdout + r.stderr
