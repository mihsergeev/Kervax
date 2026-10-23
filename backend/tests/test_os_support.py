"""Обновления и поддержка ОС: конец поддержки дистрибутива, отложенная перезагрузка, обновления
безопасности без автообновлений, давно не обновлявшийся список пакетов."""
from datetime import datetime, timedelta, timezone

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
    return [{k: v for k, v in x.items() if k not in ("fix", "new_key", "muted_new")} for x in xs]


def test_eol_advice():
    assert _nofix(os_support.advice({"os": "Ubuntu 20.04.6 LTS"}, NOW)) == [{
        "kind": "eol", "level": 2, "key": "osupd:eol", "muted": False, "text": (
            "Ubuntu 20.04: обычная поддержка кончилась 31.05.2025, дальше Ubuntu Pro (ESM до 30.04.2030) "
            "или переезд на новую LTS")}]
    assert _nofix(os_support.advice({"os": "Debian GNU/Linux 11 (bullseye)"}, NOW)) == [{
        "kind": "eol", "level": 2, "key": "osupd:eol", "muted": False,
        "text": "Debian 11: поддержка кончилась 31.08.2026, обновлений безопасности больше нет"}]
    # ESM тоже кончился - Ubuntu Pro уже не спасет
    assert [a["text"] for a in os_support.advice({"os": "Ubuntu 16.04.7 LTS"}, NOW)] == [
        "Ubuntu 16.04: поддержка кончилась 30.04.2021, ESM тоже (30.04.2026), обновлений безопасности больше нет"]
    # за полгода предупреждаем, раньше молчим
    soon = os_support.advice({"os": "Ubuntu 22.04.4 LTS"}, datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert [(a["level"], a["text"]) for a in soon] == [(1, "Ubuntu 22.04: поддержка кончится 01.06.2027, через 151 день")]
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
    # обновлен только dbus: сначала почему не перезапуск dbus, а перезагрузка при случае
    assert fix["reboot"] == [{"id": "reboot_soft"},
                             {"id": "noboot_docker", "cmd": "sudo docker update --restart unless-stopped cadvisor"},
                             {"id": "reboot", "cmd": "sudo reboot"}]
    # Ubuntu после обычной поддержки: Pro или переезд
    assert [(s["id"], s.get("alt", False)) for s in fix["eol"]] == [
        ("pro_attach", False), ("unatt_install", False), ("pro_run", False), ("release_upgrade", True)]
    assert fix["eol"][0]["arg"] == "30.04.2030"
    # заглушенный контейнер в шаги не идет; автообновления выключены, а не отсутствуют - без установки
    rep2 = dict(rep, os_upd=dict(rep["os_upd"], unatt="off"))
    fix2 = {a["kind"]: a["fix"] for a in os_support.advice(rep2, NOW, ["noboot:cadvisor"])}
    assert fix2["reboot"] == [{"id": "reboot_soft"}, {"id": "reboot", "cmd": "sudo reboot"}]
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
        (1, "Ubuntu 20.04: исправления Ubuntu Pro (ESM) кончатся 30.04.2030, через 150 дней")]
    assert late[0]["fix"] == [{"id": "release_upgrade", "cmd": "sudo do-release-upgrade", "alt": False}]
    # ESM кончился - как без подписки
    assert os_support.advice({"os": "Ubuntu 16.04.7 LTS", "os_upd": _upd(esm=True)}, NOW)[0]["level"] == 2


def test_decision_mutes_only_its_kind():
    rep = {"os": "Ubuntu 20.04.6 LTS", "os_upd": _upd(reboot=True, reboot_ts=T - 30 * DAY, security=4, unatt="off")}
    got = os_support.advice(rep, NOW, ["osupd:eol", "osupd:reboot", "dockrisk:x"])
    assert [(a["key"], a["muted"]) for a in got] == [
        ("osupd:eol", True), ("osupd:reboot", True), ("osupd:security", False)]
    assert all(not a["muted"] for a in os_support.advice(rep, NOW, None))


def _reboot(rep, mutes=None):
    return [a for a in os_support.advice(rep, NOW, mutes) if a["kind"] == "reboot"][0]


def test_reboot_muted_until_new():
    """Заглушка до нового глушит ровно этот набор пакетов: новый пакет, новая установка (файл
    переписан), перезагрузка и снова "нужна", новое ядро в /boot - пункт снова виден."""
    rep = {"os_upd": _upd(reboot=True, reboot_ts=T - 12 * DAY, reboot_pkgs=["linux-image-6.8.0-47-generic", "linux-base"])}
    a = _reboot(rep)
    key = a["new_key"]
    assert key.startswith("osupd:reboot@") and len(key) == len("osupd:reboot@") + 10
    assert not a["muted"] and not a["muted_new"]
    a = _reboot(rep, [key])
    assert a["muted"] and a["muted_new"] and a["key"] == "osupd:reboot"
    # порядок и повторы в .pkgs отпечаток не меняют
    same = {"os_upd": dict(rep["os_upd"], reboot_pkgs=["linux-base", "linux-image-6.8.0-47-generic", "linux-base"])}
    assert _reboot(same, [key])["muted"]
    for other in (
        dict(rep["os_upd"], reboot_pkgs=["linux-image-6.8.0-47-generic", "linux-base", "libc6"]),
        dict(rep["os_upd"], reboot_ts=T - 8 * DAY),
    ):
        b = _reboot({"os_upd": other}, [key])
        assert not b["muted"] and b["new_key"] != key
    k = {"os_upd": _upd(kernel_new="6.8.0-47-generic")}
    assert _reboot(k)["new_key"] != key and _reboot(k, [_reboot(k)["new_key"]])["muted"]
    # решение "перезагружать нельзя" главнее заглушки
    both = _reboot(rep, ["osupd:reboot", key])
    assert both["muted"] and not both["muted_new"]


def test_stale_reboot_mutes():
    rep = {"os_upd": _upd(reboot=True, reboot_ts=T - 12 * DAY, reboot_pkgs=["linux-image-6.8.0-47-generic"])}
    key = _reboot(rep)["new_key"]
    assert os_support.stale_mutes(rep, [key, "osupd:reboot", "disk_health"]) == []
    newer = {"os_upd": dict(rep["os_upd"], reboot_ts=T - DAY, reboot_pkgs=["linux-image-6.8.0-47-generic", "libc6"])}
    assert os_support.stale_mutes(newer, [key, "osupd:reboot"]) == [
        (key, "после заглушки снова нужна перезагрузка (обновлены linux-image-6.8.0-47-generic, libc6)")]
    assert os_support.stale_mutes({"os_upd": _upd()}, [key]) == [(key, "перезагрузка больше не нужна")]
    assert os_support.stale_mutes({"os_upd": _upd(kernel_new="6.8.0-50-generic")}, [key]) == [
        (key, "в /boot новое ядро 6.8.0-50-generic")]
    # нет данных об обновлениях - не трогаем
    assert os_support.stale_mutes({}, [key]) == []


def test_reboot_reason_in_plain_words():
    """Зачем перезагрузка: ядро - что работает и что заработает (самое новое из установленных),
    linux-base не показываем, библиотеки и прочее - своими словами; полный список - в pkgs."""
    def text(pkgs, kernel="6.8.0-40-generic"):
        return _reboot({"os_upd": _upd(reboot=True, reboot_ts=T - 7 * DAY, reboot_pkgs=pkgs, kernel=kernel)})

    a = text(["linux-image-6.8.0-86-generic", "linux-base", "linux-image-6.14.0-34-generic", "linux-image-6.8.0-87-generic"])
    assert a["text"] == ("работает ядро 6.8.0-40, установлено 6.14.0-34 - новое заработает после перезагрузки "
                         "(последнее обновление 7 дней назад)")
    assert "linux-base" in a["pkgs"] and len(a["pkgs"]) == 4
    assert text(["libc6", "libssl3", "libsystemd0", "libudev1"])["text"].startswith(
        "обновлены libc6, libssl3, libsystemd0 и еще 1 - процессы держат старые в памяти")
    assert text(["linux-firmware"])["text"].startswith("обновлен linux-firmware - нужна перезагрузка (")
    # уже работает самое новое, а файл просит перезагрузку только из-за linux-base: без подробностей
    assert text(["linux-base"])["text"] == "нужна перезагрузка после обновления (последнее обновление 7 дней назад)"


def test_dbus_only_is_soft():
    """Обновлен только dbus: не "нужна перезагрузка", а при случае, и не раньше месяца."""
    rep = {"os_upd": _upd(reboot=True, reboot_ts=T - 1590 * DAY, reboot_pkgs=["dbus", "libdbus-1-3"])}
    a = _reboot(rep)
    assert a["soft"] and a["level"] == 1
    assert a["text"] == ("обновлен dbus, перезапустить его без перезагрузки нельзя - перезагрузить при случае "
                         "(последнее обновление 1590 дней назад)")
    assert a["pkgs"] == ["dbus", "libdbus-1-3"]
    assert os_support.advice({"os_upd": dict(rep["os_upd"], reboot_ts=T - 20 * DAY)}, NOW) == []
    # dbus вместе с ядром - обычная перезагрузка
    mixed = _reboot({"os_upd": dict(rep["os_upd"], reboot_pkgs=["dbus", "linux-base"])})
    assert not mixed.get("soft") and mixed["text"] == (
        "обновлен dbus, перезапустить его без перезагрузки нельзя (последнее обновление 1590 дней назад)")
    assert mixed["fix"] == [{"id": "reboot", "cmd": "sudo reboot"}]
    # без списка пакетов неизвестно, что обновили - обычная
    assert not _reboot({"os_upd": dict(rep["os_upd"], reboot_pkgs=[])}).get("soft")


async def test_collector_lifts_stale_reboot_mute(tmp_path):
    """Сборщик убирает заглушку, которая уже не про то, что на ноде, и пишет это в журнал."""
    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import AuditLog, Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'rb.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    old = {"os_upd": _upd(reboot=True, reboot_ts=ts - 12 * DAY, reboot_pkgs=["linux-image-6.8.0-47-generic"])}
    key = os_support.REBOOT_NEW + os_support.reboot_sig(old)

    def report(upd: dict) -> dict:
        return {"clock_unix": ts, "uptime_seconds": 100000, "os_upd": upd}

    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, created_at=now - timedelta(days=30),
                     alert_mutes=[key, "osupd:security"], last_report=report(old["os_upd"])))
        await s.commit()
    cfg = Settings()
    await collector.evaluate_servers(factory, cfg, now)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert srv.alert_mutes == [key, "osupd:security"]
        srv.last_report = report(_upd(reboot=True, reboot_ts=ts - DAY,
                                      reboot_pkgs=["linux-image-6.8.0-47-generic", "linux-image-6.8.0-50-generic"]))
        await s.commit()
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=1))
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert srv.alert_mutes == ["osupd:security"]
        rows = list(await s.scalars(select(AuditLog).where(AuditLog.action == "mute_lifted")))
    assert [(r.username, r.target, r.detail) for r in rows] == [(
        "авто", "node-a", "заглушка перезагрузки снята: после заглушки снова нужна перезагрузка "
        "(обновлены linux-image-6.8.0-47-generic, linux-image-6.8.0-50-generic)")]
    await engine.dispose()


def test_healthy_updates_say_nothing():
    assert os_support.advice({"os": "Ubuntu 24.04.1 LTS", "os_upd": _upd(updates=12, security=3)}, NOW) == []
    # перезагрузку пару дней откладывают - это нормально
    assert os_support.advice({"os_upd": _upd(reboot=True, reboot_ts=T - 2 * DAY)}, NOW) == []


def test_each_update_advice():
    rep = {"os_upd": _upd(reboot=True, reboot_ts=T - 12 * DAY, reboot_pkgs=["linux-image-6.8.0-47-generic", "libc6"],
                          security=5, unatt="off", apt_ts=T - 20 * DAY)}
    assert [a["text"] for a in os_support.advice(rep, NOW)] == [
        "работает ядро 6.8.0-45, установлено 6.8.0-47 - новое заработает после перезагрузки; обновлена libc6 - "
        "процессы держат старую в памяти, нужна перезагрузка или перезапуск служб (последнее обновление 12 дней назад)",
        "не установлено 5 обновлений безопасности: автообновления выключены",
        "список пакетов не обновлялся 20 дней: о новых обновлениях нода не знает",
    ]
    rep = {"os_upd": _upd(kernel_new="6.8.0-47-generic", security=1, unatt="on", unatt_ts=T - 5 * DAY)}
    assert [a["text"] for a in os_support.advice(rep, NOW)] == [
        "работает ядро 6.8.0-45, установлено 6.8.0-47 - новое заработает после перезагрузки",
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
