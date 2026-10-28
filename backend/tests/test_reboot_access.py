"""Не переживет перезагрузку (контейнеры, юниты) и доступы (заброшенные учетки, слабые ключи, второй root)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import access_audit, collector, os_support, reboot_survive
from app.models import Server

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
DAY = 86400
AGENT = Path(__file__).resolve().parents[2] / "agent"


def _srv(rep, mutes=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _c(name, policy, status="Up 3 weeks", image="app/x:1", state="running"):
    return {"name": name, "image": image, "state": state, "policy": policy, "status": status}


def test_containers_without_restart_policy():
    rep = {"docker": {"access": True, "containers": [
        _c("cadvisor", "", "Up 2 years (healthy)", "gcr.io/cadvisor/cadvisor:v0.47.2"),
        _c("n8n", "no", "Up 5 days"),
        _c("job", "on-failure", "Up 2 days"),
        _c("web", "unless-stopped"), _c("db", "always"),
        _c("fresh", "no", "Up 3 hours"),                                  # меньше суток - разовое
        _c("ch_a.1.lx1h0vvec35poxabcdefghijk", "no"),                      # задача swarm
        _c("k8s_app_pod", "no"),                                           # под Kubernetes
        _c("coolify-sentinel", "no", image="ghcr.io/coollabsio/sentinel:1.0.2"),
        _c("old", "no", "Exited (0) 2 days ago", state="exited"),
    ]}}
    assert [(c["name"], c["policy"]) for c in reboot_survive.containers(rep)] == [
        ("cadvisor", "no"), ("n8n", "no"), ("job", "on-failure")]
    assert reboot_survive.containers({"docker": {"access": False, "containers": [_c("a", "no")]}}) == []


def _units(*noboot, ts=TS):
    return {"extras": {"units": {"v": 1, "ts": ts, "units": [], "restarting": [], "noboot": list(noboot)}}}


def test_units_started_without_enable():
    rep = _units({"unit": "myapp.service", "started": TS - 3 * DAY}, {"unit": "debug.service", "started": TS - 600},
                 {"unit": "getty@tty6.service", "started": TS - 40 * DAY})  # консоль по требованию, не служба
    assert reboot_survive.units(rep, NOW) == [{"unit": "myapp.service", "started": TS - 3 * DAY}]
    assert reboot_survive.units(_units({"unit": "x.service", "started": 0}, ts=TS - 2 * 3600), NOW) == []  # helper молчит


def test_noboot_view_advice_and_mutes():
    rep = {**_units({"unit": "myapp.service", "started": TS - 3 * DAY}),
           "docker": {"access": True, "containers": [_c("n8n", "no"), _c("cadvisor", "")]}}
    assert reboot_survive.advice(_srv(rep), NOW) == {"docker": ["n8n", "cadvisor"], "units": ["myapp.service"]}
    s = _srv(rep, mutes=["noboot:cadvisor", "noboot:myapp.service"])
    assert reboot_survive.advice(s, NOW) == {"docker": ["n8n"], "units": []}
    assert [c["muted"] for c in reboot_survive.view(s, NOW)["docker"]] == [False, True]
    assert reboot_survive.advice(_srv(rep, mutes=["noboot"]), NOW) is None


def test_container_started_by_systemd_unit():
    """docker run --rm из юнита systemd: restart no, но после ребута его поднимет systemd"""
    rep = {"clock_unix": TS, "docker": {"access": True, "containers": [
        {**_c("edge-proxy", "no", image="haproxy:3.0-alpine"), "auto_rm": True,
         "unit": "edge-proxy.service", "unit_on": True},                        # юнит включен
        {**_c("worker-1", "no"), "auto_rm": True, "unit": "worker.service"},   # юнит не включен
        {**_c("adhoc", "no"), "auto_rm": True},                                 # --rm, юнита нет
        _c("cadvisor", ""),                                                     # обычный без policy
    ]}}
    assert reboot_survive.containers(rep) == [
        {"name": "adhoc", "image": "app/x:1", "policy": "no", "autorm": True},
        {"name": "cadvisor", "image": "app/x:1", "policy": "no", "autorm": False}]
    # без helper'а units-setup не включенный юнит контейнера - пункт про юнит
    assert reboot_survive.units(rep, NOW) == [{"unit": "worker.service", "started": 0, "container": "worker-1"}]
    assert reboot_survive.advice(_srv(rep), NOW) == {"docker": ["adhoc", "cadvisor"], "units": ["worker.service"]}
    v = reboot_survive.view(_srv(rep, mutes=["noboot:worker.service"]), NOW)
    assert v["units"] == [{"unit": "worker.service", "started": 0, "container": "worker-1", "muted": True}]
    # helper стоит: про юниты решает он (worker.service тянет таймер - в его списке нет)
    assert reboot_survive.units({**rep, **_units()}, NOW) == []
    # перед перезагрузкой docker update только для обычного: на --rm докер его не примет
    fix = os_support._fix("reboot", rep, NOW, set())
    assert {"id": "noboot_docker", "cmd": "sudo docker update --restart unless-stopped cadvisor"} in fix
    assert {"id": "noboot_units", "cmd": "sudo systemctl enable worker.service"} in fix
    # старый агент без полей: все как раньше
    assert [c["name"] for c in reboot_survive.containers({"docker": {"access": True, "containers": [
        _c("edge-proxy", "no")]}})] == ["edge-proxy"]


def _snap(users, keys=(), nopasswd=(), groups=None, since=TS - 5 * 365 * DAY, ssh_pass="no"):
    return {"extras": {"changes": {
        "v": 1, "ts": TS, "seen_since": since, "users": list(users), "keys": list(keys),
        "sudo": {"groups": groups or {}, "files": {}, "nopasswd": list(nopasswd)},
        "ssh": {"pass": ssh_pass, "kbd": "no", "pam": "yes", "methods": "any", "root": "prohibit-password",
                "ports": [22], "fail": 0, "accp": 0, "guard": ""}}}}


def _u(name, uid=1000, pw="L", last=0):
    return {"n": name, "uid": uid, "sh": "/bin/bash", "pw": pw, "last": last}


def _k(user, t="ED25519", b=256, c=""):
    return {"u": user, "t": t, "b": b, "fp": f"SHA256:{user}{t}{b}", "c": c, "f": f"/home/{user}/.ssh/authorized_keys"}


def test_stale_accounts_need_a_long_enough_look():
    users = [_u("ops", last=TS - DAY), _u("olduser", pw="P", last=TS - 4 * 365 * DAY),
             _u("ci", last=0), _u("keyless", pw="P", last=0), _u("svc", last=TS - 100 * DAY)]
    keys = [_k("ops"), _k("olduser", "RSA", 3072), _k("ci"), _k("svc")]
    rep = _snap(users, keys, nopasswd=["%sudo"], groups={"sudo": ["olduser"]})
    acc = {a["name"]: a for a in access_audit.accounts(_srv(rep), NOW)[0]}
    assert acc["ops"]["stale"] is False                       # входил вчера
    assert acc["olduser"]["stale"] and acc["olduser"]["nopasswd"]  # ушедший сотрудник с sudo
    assert acc["ci"]["stale"]                                  # ключ есть, за 5 лет журнала входов нет
    assert acc["keyless"]["access"] is False and not acc["keyless"]["stale"]  # пароль, но вход по паролю выключен
    assert acc["svc"]["stale"]                                 # 100 дней без входов при длинном журнале
    assert access_audit.advice(_srv(rep), NOW) == {"stale": ["olduser", "ci", "svc"], "weak": []}
    # журнал всего неделю: судить рано
    short = _snap(users, keys, since=TS - 7 * DAY)
    assert access_audit.advice(_srv(short), NOW) is None
    # две недели журнала: только те, кто по терминалу не входил больше года (или никогда)
    two = _snap(users, keys, since=TS - 15 * DAY)
    assert access_audit.advice(_srv(two), NOW) == {"stale": ["olduser", "ci"], "weak": []}
    # учетка нужна - заглушена точечно
    assert access_audit.advice(_srv(rep, mutes=["access:ci", "access:svc"]), NOW) == {"stale": ["olduser"], "weak": []}
    # пароль считается доступом, когда SSH пускает по паролю
    pw_on = _snap([_u("keyless", pw="P", last=0)], ssh_pass="yes")
    assert access_audit.advice(_srv(pw_on), NOW) == {"stale": ["keyless"], "weak": []}


def test_weak_keys_and_old_helper():
    rep = _snap([_u("ops", last=TS - DAY)], [_k("ops", "RSA", 1024, "old laptop"), _k("ops", "DSA", 1024), _k("ops", "RSA", 2048)])
    a = access_audit.accounts(_srv(rep), NOW)[0][0]
    assert a["weak"] == ["RSA-1024 old laptop", "DSA-1024"]
    assert access_audit.advice(_srv(rep), NOW) == {"stale": [], "weak": ["ops"]}
    old = _snap([_u("ops")])
    del old["extras"]["changes"]["seen_since"]
    assert access_audit.accounts(_srv(old), NOW) is None and access_audit.advice(_srv(old), NOW) is None


def test_second_root_and_empty_password_alert():
    rep = _snap([_u("root", uid=0, pw="L"), _u("toor", uid=0, pw="P"), _u("guest", pw="NP")])
    assert [lvl for lvl, _ in access_audit.problems(_srv(rep))] == [2, 2]
    lvl, ctx = collector._server_conditions(_srv(rep), NOW)["access_risk"]
    assert lvl == 2 and "toor" in ctx["detail"] and "guest" in ctx["detail"]
    rows = [r for r in collector.server_problems(_srv(rep), NOW) if r["kind"] == "access_risk"]
    assert len(rows) == 2 and rows[0]["sec"] == "access"
    assert collector._server_conditions(_srv(_snap([_u("root", uid=0)])), NOW)["access_risk"][0] == 0


def _root_snap(permit, keys=(), ssh_pass="yes"):
    rep = _snap([_u("root", uid=0, pw="P"), _u("ops", pw="L", last=TS - DAY)], [_k("ops"), *keys], ssh_pass=ssh_pass)
    rep["extras"]["changes"]["ssh"].update(root=permit, rootpass=ssh_pass, rootkbd="no")
    return rep


def test_root_access_follows_permit_root_login():
    """root с паролем при PermitRootLogin no или prohibit-password по паролю не войдет: это не доступ"""
    def root(rep):
        return {a["name"]: a for a in access_audit.accounts(_srv(rep), NOW)[0]}["root"]
    assert root(_root_snap("no"))["access"] is False
    assert root(_root_snap("prohibit-password"))["access"] is False
    assert root(_root_snap("yes"))["access"] and root(_root_snap("yes"))["stale"]
    rk = {**_k("root"), "f": "/root/.ssh/authorized_keys"}
    assert root(_root_snap("prohibit-password", [rk]))["access"] is True  # по ключу пускают
    assert root(_root_snap("no", [rk]))["access"] is False


def _fix(rep, name, mutes=None):
    return {a["name"]: a for a in access_audit.view(_srv(rep, mutes), NOW)["accounts"]}[name].get("fix")


def test_disable_stale_root():
    """root: только passwd -l (срок учетки ломает cron root и su), ключи переименовать одной командой, вернуть - обратно"""
    rk = {**_k("root"), "f": "/root/.ssh/authorized_keys"}
    fix = _fix(_root_snap("yes", [rk]), "root")
    assert [(x["id"], x.get("cmd")) for x in fix] == [
        ("acc_root_off", "sudo passwd -l root &&\nsudo mv /root/.ssh/authorized_keys /root/.ssh/authorized_keys.disabled"),
        ("acc_undo", "sudo passwd -u root\nsudo mv /root/.ssh/authorized_keys.disabled /root/.ssh/authorized_keys")]
    assert not any("usermod" in (x.get("cmd") or "") for x in fix)
    # пускают только по ключу: пароль root (вход в консоль) не трогаем
    assert [(x["id"], x["cmd"]) for x in _fix(_root_snap("prohibit-password", [rk]), "root")] == [
        ("acc_root_keys", "sudo mv /root/.ssh/authorized_keys /root/.ssh/authorized_keys.disabled"),
        ("acc_undo", "sudo mv /root/.ssh/authorized_keys.disabled /root/.ssh/authorized_keys")]
    # ключей нет, пускают по паролю: только блокировка пароля
    assert [(x["id"], x["cmd"]) for x in _fix(_root_snap("yes"), "root")] == [
        ("acc_root_lock", "sudo passwd -l root"), ("acc_undo", "sudo passwd -u root")]
    # после этих команд доступа нет - и пункт уходит сам
    gone = _root_snap("yes", ssh_pass="yes")
    gone["extras"]["changes"]["users"][0]["pw"] = "L"
    assert _fix(gone, "root") is None and access_audit.advice(_srv(gone), NOW) is None


def test_disable_stale_account():
    users = [_u("ops", last=TS - DAY), _u("olduser", pw="P", last=TS - 400 * DAY), _u("svc", last=TS - 100 * DAY)]
    keys = [_k("ops"), _k("olduser"), {**_k("olduser"), "fp": "SHA256:x", "f": "/home/olduser/.ssh/authorized_keys2"},
            {**_k("svc"), "f": "/srv/svc home/.ssh/authorized_keys"}]
    rep = _snap(users, keys)
    fix = _fix(rep, "olduser")
    # проверка, одно действие "отключить", потом откат и удаление - их фронт сворачивает
    assert [(x["id"], x.get("alt", False), x.get("undo", False)) for x in fix] == [
        ("acc_check", False, False), ("acc_off", False, False), ("acc_undo", False, True), ("acc_delete", True, False)]
    st = {x["id"]: x["cmd"] for x in fix}
    assert st["acc_check"].splitlines() == [
        "ps -u olduser -o pid,lstart,args", "sudo crontab -l -u olduser", "ls /var/lib/systemd/linger/",
        "grep -rlxF 'User=olduser' /etc/systemd/system /lib/systemd/system"]
    # блокировка и ключи одной командой: ключи не трогаются, если usermod не прошел
    assert st["acc_off"].splitlines() == [
        "sudo usermod -L -e 1 olduser &&",
        "sudo mv /home/olduser/.ssh/authorized_keys /home/olduser/.ssh/authorized_keys.disabled &&",
        "sudo mv /home/olduser/.ssh/authorized_keys2 /home/olduser/.ssh/authorized_keys2.disabled"]
    assert st["acc_undo"].splitlines() == [
        "sudo usermod -U -e '' olduser",
        "sudo mv /home/olduser/.ssh/authorized_keys.disabled /home/olduser/.ssh/authorized_keys",
        "sudo mv /home/olduser/.ssh/authorized_keys2.disabled /home/olduser/.ssh/authorized_keys2"]
    assert st["acc_delete"] == "sudo userdel olduser"
    # пароль и так заблокирован: без -L, иначе возврат через -U не сработает; путь с пробелом - в кавычках
    st = {x["id"]: x["cmd"] for x in _fix(rep, "svc")}
    assert st["acc_off"] == ("sudo usermod -e 1 svc &&\n"
                             "sudo mv '/srv/svc home/.ssh/authorized_keys' '/srv/svc home/.ssh/authorized_keys.disabled'")
    assert st["acc_undo"].splitlines()[0] == "sudo usermod -e '' svc"
    # без ключей (вход по паролю): только блокировка, шаг со своим текстом
    pw = _snap([_u("ops", last=TS - DAY), _u("olduser", pw="P", last=TS - 400 * DAY)], [_k("ops")], ssh_pass="yes")
    st = {x["id"]: x["cmd"] for x in _fix(pw, "olduser")}
    assert st["acc_off_nokeys"] == "sudo usermod -L -e 1 olduser" and st["acc_undo"] == "sudo usermod -U -e '' olduser"
    # учетка нужна - команд нет; входят только через нее - отключать нельзя
    assert _fix(rep, "olduser", mutes=["access:olduser"]) is None and _fix(rep, "ops") is None
    alone = _snap([_u("olduser", pw="P", last=TS - 400 * DAY)], [_k("olduser")])
    assert _fix(alone, "olduser") == [{"id": "acc_alone", "arg": "olduser"}]


def test_helpers_report_the_new_fields():
    ch = (AGENT / "changes-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.7" in ch and '"seen_since":%s' in ch and '\\"pw\\":' in ch and '"nopasswd":[%s]' in ch
    un = (AGENT / "units-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.6" in un and '"noboot":[%s]' in un
    for f in ("changes-setup.sh", "units-setup.sh"):
        assert b"\r" not in (AGENT / f).read_bytes()


def test_journal_reads_cannot_hang_under_systemd():
    """systemd запускает сервис с проигнорированным SIGPIPE: "journalctl | head -1" не останавливал
    journalctl на первой строке, он дочитывал журнал в гигабайты, и юнит убивался по таймауту
    на каждом запуске (changes-setup 0.5 на нодах с большим журналом)"""
    import re
    for f in sorted(AGENT.glob("*.sh")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#") and re.search(r"\bjournalctl\b[^#]*\|\s*head\b", line):
                assert "timeout " in line, f"{f.name}: {line.strip()}"
    ch = (AGENT / "changes-setup.sh").read_text(encoding="utf-8")
    assert "IgnoreSIGPIPE=no" in ch and "journalctl -r -u ssh -u sshd" in ch
    assert '"$tmp"' in ch and ch.index('tmp=$(mktemp "${OUT}.XXXXXX")') > ch.index('lastseen_update "$now"')
