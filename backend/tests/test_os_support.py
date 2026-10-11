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


def test_eol_advice():
    assert os_support.advice({"os": "Ubuntu 20.04.6 LTS"}, NOW) == [{"kind": "eol", "level": 2, "text": (
        "обычная поддержка Ubuntu 20.04 кончилась 31.05.2025: обновления безопасности только по подписке Ubuntu Pro")}]
    assert os_support.advice({"os": "Debian GNU/Linux 11 (bullseye)"}, NOW) == [{"kind": "eol", "level": 2, "text": (
        "поддержка Debian 11 кончилась 31.08.2026: обновлений безопасности больше нет")}]
    # за полгода предупреждаем, раньше молчим
    soon = os_support.advice({"os": "Ubuntu 22.04.4 LTS"}, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert soon == [{"kind": "eol", "level": 1, "text": "поддержка Ubuntu 22.04 кончится 01.06.2027, через 151 день"}]
    assert os_support.advice({"os": "Ubuntu 24.04.1 LTS"}, NOW) == []


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
