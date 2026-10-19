"""Обновления и поддержка ОС: конец поддержки дистрибутива, отложенная перезагрузка, обновления
безопасности без автообновлений, давно не обновлявшийся список пакетов."""
from datetime import datetime, timezone

from app import os_support

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
DAY = 86400
T = int(NOW.timestamp())


def _upd(**kw):
    base = {"updates": 0, "security": 0, "apt_ts": T - DAY, "unatt": "on", "unatt_ts": T - DAY,
            "kernel": "6.8.0-45-generic", "ts": T}
    base.update(kw)
    return base


def test_eol_by_pretty_name():
    assert os_support.eol_of("Ubuntu 22.04.4 LTS")[0] == "Ubuntu 22.04"
    assert os_support.eol_of("Debian GNU/Linux 12 (bookworm)")[0] == "Debian 12"
    assert os_support.eol_of("Debian GNU/Linux 1 (x)") is None
    assert os_support.eol_of("Rocky Linux 9.3 (Blue Onyx)")[0] == "Rocky Linux 9.3"
    assert os_support.eol_of("Arch Linux") is None
    assert os_support.eol_of("") is None


def _nofix(xs):
    return [{k: v for k, v in x.items() if k != "fix"} for x in xs]


def test_eol_advice():
    assert _nofix(os_support.advice({"os": "Ubuntu 20.04.6 LTS"}, NOW)) == [{
        "kind": "eol", "level": 2, "key": "osupd:eol", "muted": False, "text": (
            "обычная поддержка Ubuntu 20.04 кончилась 31.05.2025: исправления безопасности дальше только в Ubuntu Pro "
            "(ESM до 30.04.2030, pro attach) или после переезда на новую LTS")}]
    assert _nofix(os_support.advice({"os": "Debian GNU/Linux 11 (bullseye)"}, NOW)) == [{
        "kind": "eol", "level": 2, "key": "osupd:eol", "muted": False,
        "text": "поддержка Debian 11 кончилась 31.08.2026: обновлений безопасности больше нет"}]
    # ESM тоже кончился - Ubuntu Pro уже не спасет
    assert [a["text"] for a in os_support.advice({"os": "Ubuntu 16.04.7 LTS"}, NOW)] == [
        "поддержка Ubuntu 16.04 кончилась 30.04.2021, ESM тоже (30.04.2026): обновлений безопасности больше нет"]
    # за полгода предупреждаем, раньше молчим
    soon = os_support.advice({"os": "Ubuntu 22.04.4 LTS"}, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert [(a["level"], a["text"]) for a in soon] == [(1, "поддержка Ubuntu 22.04 кончится 01.06.2027, через 151 день")]
    assert os_support.advice({"os": "Ubuntu 24.04.1 LTS"}, NOW) == []


def test_fix_steps_per_item():
    """У каждого пункта команды под ноду, после которых он уходит сам."""
    rep = {"os": "Ubuntu 20.04.4 LTS", "os_upd": _upd(security=183, unatt="none", apt_ts=T - 362 * DAY, reboot=True,
                                                     reboot_ts=T - 1586 * DAY, reboot_pkgs=["dbus"]),
           "docker": {"access": True, "containers": [
               {"name": "cadvisor", "image": "x", "state": "running", "policy": "no", "status": "Up 2 years"},
               {"name": "rest-server", "image": "y", "state": "running", "policy": "always", "status": "Up 2 months"}]}}
    fix = {a["kind"]: a["fix"] for a in os_support.advice(rep, NOW)}
    assert [s["id"] for s in fix["security"]] == ["apt_update", "unatt_install", "unatt_enable", "unatt_run"]
    assert fix["security"][-1]["cmd"] == "sudo unattended-upgrade -v"
    # агент ищет Unattended-Upgrade в начале строки: включение двумя строками
    assert fix["security"][2]["cmd"].splitlines()[1].startswith("echo 'APT::Periodic::Unattended-Upgrade \"1\";'")
    assert [s["id"] for s in fix["apt"]] == ["apt_update", "apt_daily"] and "tee -a" in fix["apt"][1]["cmd"]
    # до перезагрузки - то, что после нее не поднимется
    assert fix["reboot"] == [{"id": "noboot_docker", "cmd": "sudo docker update --restart unless-stopped cadvisor"},
                             {"id": "reboot", "cmd": "sudo reboot"}]
    # Ubuntu после обычной поддержки: Pro или переезд
    assert [(s["id"], s.get("alt", False)) for s in fix["eol"]] == [
        ("pro_attach", False), ("unatt_install", False), ("pro_run", False), ("release_upgrade", True)]
    assert fix["eol"][0]["arg"] == "30.04.2030"
    # заглушенный контейнер в шаги не идет; автообновления выключены, а не отсутствуют - без установки
    rep2 = dict(rep, os_upd=dict(rep["os_upd"], unatt="off"))
    fix2 = {a["kind"]: a["fix"] for a in os_support.advice(rep2, NOW, ["noboot:cadvisor"])}
    assert fix2["reboot"] == [{"id": "reboot", "cmd": "sudo reboot"}]
    assert [s["id"] for s in fix2["security"]] == ["apt_update", "unatt_enable", "unatt_run"]
    # включены, но не запускаются - проверить таймеры
    rep3 = dict(rep, os_upd=dict(rep["os_upd"], unatt="on", unatt_ts=T - 9 * DAY))
    assert [s["id"] for s in {a["kind"]: a["fix"] for a in os_support.advice(rep3, NOW)}["security"]] == [
        "unatt_check", "unatt_run"]
    assert os_support.advice({"os": "Debian GNU/Linux 11 (bullseye)"}, NOW)[0]["fix"] == [{"id": "debian_upgrade", "arg": "12"}]
    assert os_support.advice({"os": "CentOS Linux 7 (Core)"}, NOW)[0]["fix"] == [{"id": "el_upgrade"}]
    soon = os_support.advice({"os": "Ubuntu 22.04.4 LTS"}, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert soon[0]["fix"] == [{"id": "release_upgrade", "cmd": "sudo do-release-upgrade", "alt": False}]


def test_ubuntu_pro_attached_moves_the_end_to_esm():
    """Агент 2.39 видит репозиторий ESM: конец поддержки - конец ESM, раньше чем за полгода молчим."""
    rep = {"os": "Ubuntu 20.04.6 LTS", "os_upd": _upd(esm=True)}
    assert os_support.advice(rep, NOW) == []
    late = [a for a in os_support.advice(rep, datetime(2029, 12, 1, tzinfo=timezone.utc)) if a["kind"] == "eol"]
    assert [(a["level"], a["text"]) for a in late] == [
        (1, "исправления Ubuntu Pro (ESM) для Ubuntu 20.04 кончатся 30.04.2030, через 150 дней")]
    assert late[0]["fix"] == [{"id": "release_upgrade", "cmd": "sudo do-release-upgrade", "alt": False}]
    # ESM кончился - как без подписки
    assert os_support.advice({"os": "Ubuntu 16.04.7 LTS", "os_upd": _upd(esm=True)}, NOW)[0]["level"] == 2


def test_decision_mutes_only_its_kind():
    rep = {"os": "Ubuntu 20.04.6 LTS", "os_upd": _upd(reboot=True, reboot_ts=T - 30 * DAY, security=4, unatt="off")}
    got = os_support.advice(rep, NOW, ["osupd:eol", "osupd:reboot", "dockrisk:x"])
    assert [(a["key"], a["muted"]) for a in got] == [
        ("osupd:eol", True), ("osupd:reboot", True), ("osupd:security", False)]
    assert all(not a["muted"] for a in os_support.advice(rep, NOW, None))


def test_healthy_updates_say_nothing():
    assert os_support.advice({"os": "Ubuntu 24.04.1 LTS", "os_upd": _upd(updates=12, security=3)}, NOW) == []
    # перезагрузку пару дней откладывают - это нормально
    assert os_support.advice({"os_upd": _upd(reboot=True, reboot_ts=T - 2 * DAY)}, NOW) == []


def test_each_update_advice():
    rep = {"os_upd": _upd(reboot=True, reboot_ts=T - 12 * DAY, reboot_pkgs=["linux-image-6.8.0-47-generic", "libc6"],
                          security=5, unatt="off", apt_ts=T - 20 * DAY)}
    assert [a["text"] for a in os_support.advice(rep, NOW)] == [
        "нужна перезагрузка уже 12 дней (обновлены linux-image-6.8.0-47-generic, libc6)",
        "не установлено 5 обновлений безопасности: автообновления выключены",
        "список пакетов не обновлялся 20 дней: о новых обновлениях нода не знает",
    ]
    rep = {"os_upd": _upd(kernel_new="6.8.0-47-generic", security=1, unatt="on", unatt_ts=T - 5 * DAY)}
    assert [a["text"] for a in os_support.advice(rep, NOW)] == [
        "в /boot ядро 6.8.0-47-generic, а работает 6.8.0-45-generic: нужна перезагрузка",
        "не установлено 1 обновление безопасности: автообновления не запускались 5 дней",
    ]
    rep = {"os_upd": _upd(security=2, unatt="none")}
    assert [a["text"] for a in os_support.advice(rep, NOW)] == [
        "не установлено 2 обновления безопасности: unattended-upgrades не установлен"]
    # мусор не роняет карточку
    assert os_support.advice({"os_upd": {"security": "x", "reboot": True, "reboot_ts": "y", "apt_ts": None}}, NOW) == []


async def test_api_gives_advice(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "old-os"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    now = datetime.now(timezone.utc).timestamp()
    rep = {"hostname": "old-os", "agent_version": "2.26", "os": "Debian GNU/Linux 10 (buster)",
           "uptime_seconds": 1000, "cpu_percent": 1, "mem_used": 1, "mem_total": 2, "disks": [],
           "os_upd": {"updates": 3, "security": 2, "unatt": "off", "apt_ts": int(now - 3600), "ts": int(now)}}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    srv = next(x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid)
    assert [a["kind"] for a in srv["os_advice"]] == ["eol", "security"]
    assert srv["last_report"]["os_upd"]["security"] == 2
