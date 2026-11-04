"""Резервный сервер бэкапов (app/bsrv_mirror.py, api/bsrv_mirror.py, agent/mirror-setup.sh).

Выбранные репозитории основного сервера бэкапов копируются на отдельный резервный. Резервный забирает
их сам учеткой только для чтения на основном, панель только называет, что копировать: удалить копию,
сменить адрес известного основного и разморозить копию она не может."""
import asyncio
import io
import os
import re
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import bsrv_mirror as bm
from app import collector
from app.api import bsrv_mirror as api
from app.models import BackupCommand
from app.setup_scripts import setup_needed

AGENT = Path(__file__).resolve().parents[2] / "agent"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE1B7YqQzg7XNLJ2sx2hrF9SolyUDQG5d6BuG9qxn2rh"
HOSTKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyExampleOnlyForTestsAAAAAAAAAAAA"
BASE = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.57", "cpu_percent": 1.0,
        "mem_used": 1, "mem_total": 2, "disks": []}


def _primary_report(repos=("web-a", "db-b"), mirrors=None) -> dict:
    now = int(time.time())
    return {**BASE, "clock_unix": now, "setup_versions": {"backupserver-setup": "0.38"},
            "backup_server": {"present": True, "repos": [{"name": r, "valid": True, "snapshots": 3} for r in repos]},
            "extras": {"backup-server": {"v": 1, "ts": now, "repos": {}, "ssh": {"port": 2222, "hostkey": HOSTKEY},
                                        "mirrors": mirrors or {}}}}


def _mirror_report(copies=None, sources=None, **extra) -> dict:
    now = int(time.time())
    return {**BASE, "clock_unix": now, "setup_versions": {"mirror-setup": "0.2"},
            "extras": {"backup-mirror": {"v": 1, "ts": now, "helper": "0.2", "pubkey": PUB, "copies": copies or [],
                                        "sources": sources or {}, "running": False, "current": "", **extra}}}


async def _server(client, auth_headers, name) -> tuple[int, str]:
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"], r.json()["token"]


async def _report(client, token, rep, xff):
    r = await client.post("/api/agent/report", json=rep,
                          headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": f"{xff}, 172.18.0.1"})
    assert r.status_code == 200, r.text


def _fake_commands(monkeypatch) -> list[tuple[int, str, dict]]:
    sent: list[tuple[int, str, dict]] = []

    async def create(_factory, sid, action, payload):
        sent.append((sid, action, dict(payload)))
        return len(sent)

    async def wait(_factory, cid, _timeout):
        return True, f"OK {cid}"

    monkeypatch.setattr(api, "_bcmd_create", create)
    monkeypatch.setattr(api, "_bcmd_wait", wait)
    return sent


async def _settle():
    for _ in range(50):
        if not api._setup_tasks:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("sync did not finish")


async def test_copy_is_chosen_sent_to_both_and_taken_back(client: httpx.AsyncClient, auth_headers, monkeypatch):
    sent = _fake_commands(monkeypatch)
    p, ptok = await _server(client, auth_headers, "Bk-01")
    m, mtok = await _server(client, auth_headers, "mirror-a")
    await _report(client, ptok, _primary_report(), "203.0.113.5")

    # пока сервер не сделали резервным, копию туда не положить
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 400 and "не резервный" in r.json()["detail"]

    # роль: helper mirror-setup попросят поставить только на него
    r = await client.post(f"/api/servers/{m}/backup/mirror-role", json={"on": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["backup_mirror_role"] is True
    r = await client.post("/api/agent/mirror-wanted", content=mtok)
    assert r.text == "yes"
    r = await client.post("/api/agent/mirror-wanted", content=ptok)
    assert r.text == "no"

    # роль есть, helper еще не стоит - ключа резервного нет
    await _report(client, mtok, {**BASE, "clock_unix": int(time.time())}, "198.51.100.20")
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 400 and "mirror-setup" in r.json()["detail"]

    await _report(client, mtok, _mirror_report(), "198.51.100.20")
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "nope", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 400 and "нет репозитория" in r.json()["detail"]
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": p},
                          headers=auth_headers)
    assert r.status_code == 400

    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 200, r.text
    e = r.json()["backup_mirror"]["web-a"]
    assert (e["mirror"], e["src"]) == (m, "bk-01")
    await _settle()
    # сначала основному - кого и к чему пустить, потом резервному - откуда и что брать
    assert sent == [
        (p, "mirror_allow", {"name": f"mirror-{m}", "repos": ["web-a"], "client_ip": "198.51.100.20", "pubkey": PUB}),
        (m, "mirror", {"mode": "set", "sources": [
            {"name": "bk-01", "host": "203.0.113.5", "port": 2222, "hostkey": HOSTKEY, "repos": ["web-a"]}]}),
    ]
    srv = next(x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == p)
    assert srv["backup_mirror_sync"][str(m)]["allow"]["ok"] is True and srv["backup_mirror_sync"][str(m)]["set"]["ok"] is True

    # второй репозиторий того же сервера - тем же именем у резервного
    sent.clear()
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "db-b", "mirror_id": m},
                          headers=auth_headers)
    assert r.json()["backup_mirror"]["db-b"]["src"] == "bk-01"
    await _settle()
    assert sent[0][2]["repos"] == ["db-b", "web-a"] and sent[1][2]["sources"][0]["repos"] == ["db-b", "web-a"]

    # роль не снять, пока на сервер что-то копируется
    r = await client.post(f"/api/servers/{m}/backup/mirror-role", json={"on": False}, headers=auth_headers)
    assert r.status_code == 400

    # снять копирование: основному - меньше, резервному - без репозитория (у него копия останется)
    sent.clear()
    await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "db-b", "mirror_id": None}, headers=auth_headers)
    await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": None}, headers=auth_headers)
    await _settle()
    assert sent[-2:] == [(p, "mirror_allow", {"name": f"mirror-{m}", "repos": []}),
                         (m, "mirror", {"mode": "set", "sources": []})]
    r = await client.post(f"/api/servers/{m}/backup/mirror-role", json={"on": False}, headers=auth_headers)
    assert r.status_code == 200


async def test_only_admin_and_old_agent_refused(client: httpx.AsyncClient, auth_headers, monkeypatch):
    _fake_commands(monkeypatch)
    p, ptok = await _server(client, auth_headers, "bk-01")
    m, mtok = await _server(client, auth_headers, "mirror-a")
    await client.post(f"/api/servers/{m}/backup/mirror-role", json={"on": True}, headers=auth_headers)
    await _report(client, ptok, {**_primary_report(), "agent_version": "2.56"}, "203.0.113.5")
    await _report(client, mtok, _mirror_report(), "198.51.100.20")
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 400 and "2.57" in r.json()["detail"]
    old = _primary_report()
    old["setup_versions"] = {"backupserver-setup": "0.37"}
    await _report(client, ptok, old, "203.0.113.5")
    r = await client.post(f"/api/servers/{p}/backup/repo-mirror", json={"repo": "web-a", "mirror_id": m},
                          headers=auth_headers)
    assert r.status_code == 400 and "0.38" in r.json()["detail"]


async def test_bulk_selection_and_pull_after_snapshot(client, auth_headers, monkeypatch):
    sent = _fake_commands(monkeypatch)
    primary, ptok = await _server(client, auth_headers, "source-a")
    mirror, mtok = await _server(client, auth_headers, "mirror-a")
    url = f"/api/servers/{primary}/backup/repo-mirrors"
    await client.post(f"/api/servers/{mirror}/backup/mirror-role", json={"on": True}, headers=auth_headers)
    p = _primary_report(repos=("web-a", "db-b", "other"), mirrors={bm.mirror_name(mirror): {"repos": ["web-a", "db-b"]}})
    for repo in p["backup_server"]["repos"]:
        repo["last_activity"] = 0
    await _report(client, ptok, p, "203.0.113.5")
    await _report(client, mtok, _mirror_report(helper="0.1"), "198.51.100.20")
    r = await client.post(url, json={"repos": ["web-a", "db-b"], "mirror_id": mirror}, headers=auth_headers)
    assert r.status_code == 400 and "0.2" in r.json()["detail"]
    assert not sent
    await _report(client, mtok, _mirror_report(), "198.51.100.20")
    r = await client.post(url, json={"repos": ["web-a", "missing"], "mirror_id": mirror}, headers=auth_headers)
    assert r.status_code == 400 and not sent
    r = await client.post(url, json={"repos": ["web-a", "db-b", "web-a"], "mirror_id": mirror}, headers=auth_headers)
    assert r.status_code == 200 and set(r.json()["backup_mirror"]) == {"web-a", "db-b"}
    await _settle()
    assert len(sent) == 2 and sent[0][2]["repos"] == ["db-b", "web-a"]

    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    sources = {"source-a": {"repos": ["web-a", "db-b"]}}
    tick = lambda: bm.queue_pulls(factory, now, collector.seen_online)
    await _report(client, mtok, _mirror_report(sources=sources), "198.51.100.20")
    await tick()
    async with factory() as session:
        assert await session.scalar(select(BackupCommand)) is None

    # A new snapshot on an unselected repository does not trigger copying.
    stamp = int(now.timestamp())
    p["backup_server"]["repos"][2]["last_activity"] = stamp
    await _report(client, ptok, p, "203.0.113.5")
    await tick()
    async with factory() as session:
        assert await session.scalar(select(BackupCommand)) is None

    p["backup_server"]["repos"][0]["last_activity"] = stamp
    await _report(client, ptok, p, "203.0.113.5")
    await tick()
    await tick()
    async with factory() as session:
        cmd, = list(await session.scalars(select(BackupCommand)))
        assert (cmd.server_id, cmd.action, cmd.mode) == (mirror, "mirror", "pull")
        cmd.status = "done"
        cmd.created_at = now - timedelta(minutes=11)
        await session.commit()
    await _report(client, mtok, _mirror_report(sources=sources, copies=[
        {"src": "source-a", "repo": "web-a", "src_newest": stamp},
    ]), "198.51.100.20")
    await tick()
    async with factory() as session:
        assert len(list(await session.scalars(select(BackupCommand)))) == 1

    # Stopping one repo leaves the other assignment intact and waits for new configuration.
    r = await client.post(url, json={"repos": ["db-b"], "mirror_id": None}, headers=auth_headers)
    assert r.status_code == 200 and set(r.json()["backup_mirror"]) == {"web-a"}
    await _settle()
    assert sent[-1][2]["sources"][0]["repos"] == ["web-a"]


def _srv(**kw):
    d = {"id": 1, "name": "x", "external_ip": "", "agent_ip": "", "agent_version": "2.57", "backup_mirror": None,
         "backup_mirror_role": False, "last_report": {}}
    d.update(kw)
    return SimpleNamespace(**d)


def test_reported_mirrors_get_revoked_and_src_names():
    p = _srv(last_report=_primary_report(mirrors={"mirror-7": {"ip": "198.51.100.20", "repos": ["web-a"]},
                                                  "other": {"ip": "", "repos": []}}))
    assert bm.reported_mirror_ids(p) == {7}
    assert bm.src_name("Bk 01/Main", 3) == "bk01main" and bm.src_name("...", 3) == "server-3"
    assert bm.mirror_name(12) == "mirror-12"


def test_copy_problems():
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    day = 86400

    def c(**kw):
        d = {"src": "bk-01", "repo": "web-a", "enabled": True, "added": ts - 10 * day, "last_ok": ts - 3600,
             "frozen": False, "err_code": "", "err": "", "check_bad": 0, "bad_new": 0}
        d.update(kw)
        return d

    assert bm.copy_problems({"copies": [c()]}, now) == {}
    # один пропущенный день проходит сам, два - беда, с причиной из helper'а
    assert bm.copy_problems({"copies": [c(last_ok=ts - 30 * 3600, err_code="ssh")]}, now) == {}
    lvl, txt = bm.copy_problems({"copies": [c(last_ok=ts - 3 * day, err_code="ssh",
                                               err="Permission denied (publickey).")]}, now)["bk-01/web-a"]
    assert lvl == 2 and "отстала на 3 дн." in txt and "не пустил по SSH" in txt and "publickey" in txt
    # первая перекачка идет прямо сейчас - не отставание
    assert bm.copy_problems({"running": True, "current": "bk-01/web-a",
                             "copies": [c(last_ok=0, added=ts - 2 * day)]}, now) == {}
    assert "первая копия" in bm.copy_problems({"copies": [c(last_ok=0, added=ts - 2 * day)]}, now)["bk-01/web-a"][1]
    # на паузе - не беда, заморожена - беда и на паузе
    assert bm.copy_problems({"copies": [c(enabled=False, last_ok=ts - 30 * day)]}, now) == {}
    lvl, txt = bm.copy_problems({"copies": [c(enabled=False, frozen=True,
                                               frozen_why="snapshots 9 of 12 snapshots gone at once")]}, now)["bk-01/web-a"]
    assert lvl == 2 and "заморожена" in txt and "пропала большая часть снапшотов (9 из 12)" in txt
    assert bm.frozen_text({"frozen_why": "data 3000000000 of 5000000000 bytes of data gone at once"}).endswith("(3.0 из 5.0 ГБ)")
    assert bm.frozen_text({"frozen_why": "config_changed the config of the repository changed"}).startswith("на основном сменился")
    assert bm.copy_problems({"copies": [c(check_bad=2)]}, now)["bk-01/web-a"][0] == 1
    assert "подменены" in bm.copy_problems({"copies": [c(bad_new=1)]}, now)["bk-01/web-a"][1]


def test_stale_report_is_no_report():
    old = _mirror_report(copies=[{"src": "a", "repo": "b", "enabled": True, "frozen": True}])
    old["extras"]["backup-mirror"]["ts"] -= 3600
    assert bm.mirror_report(_srv(last_report=old)) is None
    assert bm.problems(_srv(last_report=old), datetime.now(timezone.utc)) == {}


def test_alert_registered_and_links_to_the_mirror_section():
    from app import settings_store, srv_short
    assert "backup_mirror" in settings_store.SERVER_ALERT_KINDS
    assert collector._BSRV_SEC["backup_mirror"] == "mirror"
    assert srv_short.level("backup_mirror") == 2


def test_helper_only_where_the_panel_made_a_mirror():
    assert setup_needed("mirror-setup", {}, mirror=True)
    assert not setup_needed("mirror-setup", {})
    assert setup_needed("mirror-setup", {"extras": {"backup-mirror": {}}})


# -- helper'ы и агент говорят на одном языке -------------------------------------------------------

def _src(name: str) -> str:
    s = (AGENT / name).read_text(encoding="utf-8")
    assert "\r" not in s
    return s


def test_helpers_and_agent_agree_on_actions():
    main = _src("main.go")
    ms = _src("mirror-setup.sh")
    bs = _src("backupserver-setup.sh")
    # агент: mirror_allow - в спул сервера бэкапов, mirror - в спул резервного
    assert '"allow_tls", "mirror_allow":' in main and 'case "mirror":' in main
    assert 'const mirrorReqDir = "/var/lib/kervax/mirror-req"' in main
    assert 'REQ_DIR="$STATE_DIR/mirror-req"' in ms and 'RES_DIR="$STATE_DIR/mirror-res"' in ms
    assert '"action=set"' in main and '"action=pull"' in main and "set)  if out=" in ms and "pull) systemctl" in ms
    assert 'mirror_allow)     if out="$(with_storage cmd_mirror_allow' in bs
    assert re.search(r"pubkey\) pubkey=\"\$v\";; repo\) repos\+=", bs)
    # имя учетки на основном одно и то же
    assert "REMOTE_USER=kervax-mread" in ms and "MREAD_USER=kervax-mread" in bs


def test_mirror_helper_rules():
    ms = _src("mirror-setup.sh")
    assert "\nKERVAX_SETUP_VERSION=0.2  # MAJOR.MINOR" in ms
    # ставится только туда, где панель сделала сервер резервным
    assert "/api/agent/mirror-wanted" in ms
    # скрипты в /lib65, не в /usr
    assert 'HELPER_DIR=/lib65/kervax' in ms and "/usr/local" not in ms
    # перекачка не от root и в песочнице, которая пишет только в свой каталог
    assert "-p User=\"$MUSER\"" in ms and "ProtectSystem=strict" in ms and 'ReadWritePaths="$stg"' in ms
    # файл берется, только если sha256 совпал с именем, и никогда не перезаписывается
    assert 'sha256sum --check --status "$stg/sums"' in ms and 'mv -nT -- "$f" "$copy/$rel"' in ms
    # панель через спул умеет только set и pull: удалить, разморозить, сменить адрес - только руками
    spool = ms[ms.index("cmd_process_spool() {"):]
    spool = spool[:spool.index("\n}\n")]
    assert "drop" not in spool and "unfreeze" not in spool and "accept" not in spool
    # helper атомарно подменяется
    assert 'mv -f "$HELPER.new" "$HELPER"' in ms


@pytest.mark.skipif(sys.platform != "linux", reason="the mirror reader uses Linux file descriptors")
def test_serve_only_reads(tmp_path):
    bs = _src("backupserver-setup.sh")
    # sudo без pty: иначе поток tar гнется
    assert "!use_pty" in bs and 'env_keep += "SSH_ORIGINAL_COMMAND"' in bs
    # ключ резервного привязан к его адресу и к одной команде
    assert 'restrict,from="%s",command="sudo -n %s %s"' in bs
    serve = bs.split("<<'SERVE_EOF'\n", 1)[1].split("\nSERVE_EOF\n", 1)[0]
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    (allow_dir / "mirror").write_text("repo\nalias\n")
    storage = tmp_path / "storage"
    repo = storage / "repo"
    (repo / "data" / "ab").mkdir(parents=True)
    (repo / "keys").mkdir()
    (repo / "config").write_bytes(b"config")
    (repo / "data" / "ab" / ("ab" * 32)).write_bytes(b"approved")
    other = storage / "other"
    (other / "data" / "cd").mkdir(parents=True)
    secret = other / "data" / "cd" / ("cd" * 32)
    secret.write_bytes(b"unapproved fixture")
    (repo / "data" / "cd").symlink_to(secret.parent)
    (repo / "keys" / ("a" * 64)).symlink_to(secret)
    os.link(secret, repo / "keys" / ("b" * 64))
    (storage / "alias").symlink_to(other)
    helper = tmp_path / "storage-helper"
    helper.write_text(f"#!/bin/sh\nprintf '%s\\n' '{storage}'\n")
    helper.chmod(0o755)
    serve = serve.replace("/etc/kervax/mirror-allow", str(allow_dir))
    serve = serve.replace("/var/lib/kervax/mirror-served", str(tmp_path / "served"))
    serve = serve.replace("/lib65/kervax/kervax-backupserver-helper", str(helper))

    def request(command, path=""):
        return subprocess.run(
            [sys.executable, "-I", "-c", serve, "mirror"],
            env={**os.environ, "SSH_ORIGINAL_COMMAND": command},
            input=path.encode(), capture_output=True, timeout=5,
        )

    pack = "data/ab/" + "ab" * 32
    listed = request("list repo")
    assert listed.returncode == 0, listed.stderr
    assert {line.split()[0] for line in listed.stdout.decode().splitlines()} == {"config", pack, "end"}
    assert listed.stdout.splitlines()[-1] == b"end 2"
    fetched = request("get repo", f"{pack}\n")
    assert fetched.returncode == 0, fetched.stderr
    with tarfile.open(fileobj=io.BytesIO(fetched.stdout)) as archive:
        assert archive.getnames() == [pack]
        assert archive.extractfile(pack).read() == b"approved"
    for command, path in [
        ("list alias", ""),
        ("get alias", f"data/cd/{'cd' * 32}\n"),
        ("get repo", f"data/cd/{'cd' * 32}\n"),
        ("get repo", f"keys/{'a' * 64}\n"),
        ("get repo", f"keys/{'b' * 64}\n"),
        ("get repo", "../other/config\n"),
        ("list other", ""),
        ("sh", ""),
    ]:
        denied = request(command, path)
        assert denied.returncode == 2 and b"refused:" in denied.stderr
        assert b"unapproved fixture" not in denied.stdout
