"""SSH: вход по паролю на порту, открытом в интернет, root по паролю, перебор без защиты."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, ssh_audit
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _ssh(**kw):
    s = {"pass": "yes", "kbd": "no", "pam": "yes", "methods": "any", "root": "prohibit-password",
         "rootpass": "yes", "rootkbd": "no", "ports": [2221], "fail": 0, "accp": 0, "guard": "fail2ban"}
    s.update(kw)
    return s


def _ex(state="open", via="Казахстан, Алматы", own=False, port=2221, ts=NOW):
    return {"ts": ts.isoformat(), "via": via, "own": own, "ips": ["203.0.113.5"],
            "ports": [{"ip": "203.0.113.5", "port": port, "state": state, "what": "процесс sshd", "name": "SSH",
                       "risk": False}], "seen": {}}


def _srv(ssh=None, exposure=None):
    rep = {"extras": {"changes": {"v": 1, "ts": 1, "ssh": ssh if ssh is not None else _ssh()}}}
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[], exposure=exposure or _ex())


def test_password_paths():
    assert ssh_audit.password_login(_ssh())
    assert not ssh_audit.password_login(_ssh(**{"pass": "no"}))
    assert ssh_audit.password_login(_ssh(**{"pass": "no", "kbd": "yes"}))  # пароль через PAM
    assert not ssh_audit.password_login(_ssh(**{"pass": "no", "kbd": "yes", "pam": "no"}))
    assert not ssh_audit.password_login(_ssh(methods="publickey,password"))  # пароль только вторым шагом
    assert ssh_audit.password_login(_ssh(methods="publickey password"))
    assert not ssh_audit.root_password(_ssh())  # prohibit-password - только по ключу
    assert ssh_audit.root_password(_ssh(root="yes"))
    assert not ssh_audit.root_password(_ssh(root="yes", rootpass="no"))


def test_advice_only_when_open_to_the_internet():
    s = _srv()
    a = ssh_audit.advice(s, NOW)
    assert [{k: x[k] for k in ("kind", "level", "text", "key", "muted")} for x in a] == [
        {"kind": "ssh_password", "level": 1, "text": "SSH пускает по паролю, а порт 2221 открыт в интернет: оставьте "
         "вход по ключам (PasswordAuthentication no)", "key": "ssh:ssh_password", "muted": False}]
    assert ssh_audit.problems(s, NOW) == []  # пароль с fail2ban и без перебора - не авария
    # за белым списком, проверка с панели или давняя - не судим
    assert ssh_audit.advice(_srv(exposure=_ex(state="filtered")), NOW) == []
    assert ssh_audit.advice(_srv(exposure=_ex(via="панель", own=True)), NOW) == []
    assert ssh_audit.advice(_srv(exposure=_ex(ts=NOW - timedelta(days=3))), NOW) == []
    assert ssh_audit.advice(_srv(ssh=_ssh(**{"pass": "no"})), NOW) == []
    nog = ssh_audit.advice(_srv(ssh=_ssh(guard="")), NOW)[0]["text"]
    assert nog.endswith(", от перебора не защищает ни fail2ban, ни sshguard")
    assert ssh_audit.view(s, NOW) == {"ports": [2221], "password": True, "root": "prohibit-password",
                                      "root_password": False, "guard": "fail2ban", "fail": 0,
                                      "accepted_password": 0, "open": True, "fail_top": []}


def test_alert_on_root_password_and_unguarded_brute_force():
    s = _srv(ssh=_ssh(root="yes", guard="", fail=120))
    assert ssh_audit.problems(s, NOW) == [
        (2, "root может войти по SSH паролем, а порт 2221 открыт в интернет"),
        (1, "идет перебор паролей SSH: 120 неудачных входов за 10 минут, вход по паролю включен, "
            "а fail2ban и подобного нет")]
    lvl, ctx = collector._server_conditions(s, NOW)["ssh_risk"]
    assert lvl == 2 and ctx["detail"].startswith("root может войти по SSH паролем")
    rows = [(p["level"], p["sec"]) for p in collector.server_problems(s, NOW) if p["kind"] == "ssh_risk"]
    assert rows == [(2, "ssh"), (1, "ssh")]
    assert ssh_audit.advice(s, NOW)[0]["level"] == 2
    # перебор при входе только по ключам - шум, не алерт
    assert ssh_audit.problems(_srv(ssh=_ssh(**{"pass": "no", "guard": "", "fail": 500})), NOW) == []
    assert collector._server_conditions(_srv(), NOW)["ssh_risk"][0] == 0


def test_helper_reports_ssh():
    src = (Path(__file__).resolve().parents[2] / "agent" / "changes-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.7" in src and "ssh_info()" in src and "fw_info()" in src and "addr=203.0.113.1" in src
    assert "sshd -V" in src and '"fail_top":[%s]' in src and '"logins":[%s]' in src
    assert "\r" not in src


def test_brute_force_names_the_source():
    s = _srv(ssh=_ssh(guard="", fail=120, fail_top=[{"ip": "198.51.100.9", "n": 90}, {"ip": "198.51.100.10", "n": 30}]))
    assert ssh_audit.problems(s, NOW) == [
        (1, "идет перебор паролей SSH: 120 неудачных входов за 10 минут (больше всех с 198.51.100.9: 90), вход по "
            "паролю включен, а fail2ban и подобного нет")]


def test_foreign_address_is_not_judged():
    """Внешний адрес ведет на другой сервер (там OpenSSH_7.4, у ноды 9.6p1): порты по нему - не про
    эту ноду, ни алерта про SSH, ни совета"""
    ex = _ex()
    ex["banners"] = {"203.0.113.5:2221": "SSH-2.0-OpenSSH_7.4"}
    s = _srv(ssh=_ssh(root="yes", banner="OpenSSH_9.6p1"), exposure=ex)
    from app import open_ports
    assert open_ports.foreign(s) == {"203.0.113.5": {"ip": "203.0.113.5", "outside": "SSH-2.0-OpenSSH_7.4",
                                                     "node": "OpenSSH_9.6p1", "nat": False}}
    assert ssh_audit.problems(s, NOW) == [] and ssh_audit.advice(s, NOW) == []
    assert open_ports.view(s)["foreign"][0]["ip"] == "203.0.113.5"
    # это адрес, с которого приходят отчеты, а на интерфейсах его нет: исходящий NAT, там сам шлюз
    s.external_ip = "203.0.113.5"
    assert open_ports.foreign(s)["203.0.113.5"]["nat"] is True
    s.last_report["ips"] = ["203.0.113.5"]
    assert open_ports.foreign(s)["203.0.113.5"]["nat"] is False
    # та же версия (другой пакет поверх) - та же нода
    ex["banners"] = {"203.0.113.5:2221": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19"}
    assert open_ports.foreign(_srv(ssh=_ssh(banner="OpenSSH_9.6p1"), exposure=ex)) == {}


def test_brute_force_from_the_internet_proves_ssh_is_open():
    """SSH проброшен с другого адреса и порта (NAT 203.0.113.7:2224 -> нода:2221): проверка портов
    видит по внешнему адресу чужой сервер и молчит, но перебор с внешних адресов говорит сам за себя"""
    ex = _ex()
    ex["banners"] = {"203.0.113.5:2221": "SSH-2.0-OpenSSH_7.4"}
    top = [{"ip": "198.51.100.9", "n": 90}]
    s = _srv(ssh=_ssh(guard="", fail=180, fail_top=top, banner="OpenSSH_9.6p1"), exposure=ex)
    assert ssh_audit.internet_open(s, ssh_audit.block(s.last_report), NOW) is None
    assert [lvl for lvl, _ in ssh_audit.problems(s, NOW)] == [1]
    assert ssh_audit.advice(s, NOW)[0]["kind"] == "ssh_password" and ssh_audit.view(s, NOW)["open"] is True
    # перебор из своей сети (частные адреса) или редкие опечатки - не доказательство
    lan = _srv(ssh=_ssh(guard="", fail=180, fail_top=[{"ip": "10.0.0.5", "n": 180}], banner="OpenSSH_9.6p1"),
               exposure=ex)
    assert ssh_audit.problems(lan, NOW) == [] and ssh_audit.advice(lan, NOW) == []
    few = _srv(ssh=_ssh(guard="", fail=5, fail_top=top, banner="OpenSSH_9.6p1"), exposure=ex)
    assert ssh_audit.problems(few, NOW) == []


def test_ssh_open_only_to_the_check_address_is_closed():
    """Порт SSH открыт проверке только потому, что ее адрес в белом списке файрвола (changes-setup
    0.7): для интернета он закрыт"""
    s = _srv(exposure=dict(_ex(), via_ips=["198.51.100.20"]))
    assert ssh_audit.internet_open(s, ssh_audit.block(s.last_report), NOW) is True
    s.last_report["extras"]["changes"]["fw"] = {"src": "iptables", "p4": "DROP", "p6": "DROP", "idrop": "",
                                               "fdrop": "", "in": ["4 198.51.100.20/32 2221"], "fwd": []}
    assert ssh_audit.internet_open(s, ssh_audit.block(s.last_report), NOW) is False


def _people(ssh, users, keys=(), os_name="Ubuntu 24.04.4 LTS", pw_seen=None, mutes=None):
    """Нода с учетками (changes-setup 0.5): users - (имя, uid, пароль, последний вход), keys - имена"""
    s = _srv(ssh=ssh)
    ch = s.last_report["extras"]["changes"]
    ch.update(ts=int(NOW.timestamp()), seen_since=int((NOW - timedelta(days=400)).timestamp()),
              users=[{"n": n, "uid": uid, "sh": "/bin/bash", "pw": pw, "last": int(last.timestamp()) if last else 0}
                     for n, uid, pw, last in users],
              keys=[{"u": u, "t": "ED25519", "b": 256, "fp": f"SHA256:{u}", "c": "", "f": f"/home/{u}/.ssh/authorized_keys"}
                    for u in keys])
    s.last_report["os"] = os_name
    s.changes = {"ssh_pw": pw_seen or {}}
    s.alert_mutes = mutes or []
    return s


def test_password_fix_names_who_would_be_locked_out():
    """Перед выключением паролей - кому добавить ключ: пароль без ключей и входили, или входили по
    паролю при ключе. Дальше файл 00-... (раньше 50-cloud-init.conf), проверка, reload ssh, откат"""
    users = [("deploy", 1000, "P", NOW - timedelta(days=1)),     # пароль, ключей нет, входит
             ("ops", 1001, "P", NOW - timedelta(hours=2)),        # ключ есть, но входил по паролю
             ("ci", 1002, "L", NOW - timedelta(days=1)),          # только ключ
             ("old", 1003, "P", NOW - timedelta(days=200)),       # давно никто не входит
             ("root", 0, "P", None)]                              # root по паролю и так не пускают
    s = _people(_ssh(guard=""), users, keys=["ops", "ci"], pw_seen={"ops": (NOW - timedelta(hours=2)).isoformat()})
    fix = ssh_audit.advice(s, NOW)[0]["fix"]
    assert [(x["id"], x.get("arg"), x.get("alt", False)) for x in fix] == [
        ("ssh_key_add", "deploy", False), ("ssh_key_check", "ops", False), ("ssh_dropin", None, False),
        ("ssh_check", None, False), ("ssh_reload", None, False), ("ssh_undo", None, False),
        ("ssh_match", None, True), ("ssh_guard", "2221", True)]
    st = {x["id"]: x for x in fix}
    assert st["ssh_key_add"]["on"] == "11.10.2026" and st["ssh_key_add"]["cmd"] == (
        "sudo mkdir -p -m 700 ~deploy/.ssh\n"
        "echo '<public-key>' | sudo tee -a ~deploy/.ssh/authorized_keys\n"
        "sudo chown -R deploy: ~deploy/.ssh")
    assert st["ssh_dropin"]["cmd"] == "echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/00-no-password.conf"
    assert st["ssh_reload"]["cmd"] == "sudo systemctl reload ssh"
    assert st["ssh_undo"]["cmd"] == "sudo rm /etc/ssh/sshd_config.d/00-no-password.conf\nsudo systemctl reload ssh"
    assert st["ssh_match"]["cmd"].splitlines() == [
        "echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/00-no-password.conf",
        "echo 'Match Address <cidr>' | sudo tee -a /etc/ssh/sshd_config.d/00-no-password.conf",
        "echo '    PasswordAuthentication yes' | sudo tee -a /etc/ssh/sshd_config.d/00-no-password.conf"]
    assert "port = 2221" in st["ssh_guard"]["cmd"] and "backend = systemd" in st["ssh_guard"]["cmd"]
    # root, которого пускают по паролю, без ключа - тоже не войдет
    rootpw = _people(_ssh(root="yes"), [("root", 0, "P", NOW - timedelta(days=3))])
    assert [x["arg"] for x in ssh_audit.advice(rootpw, NOW)[0]["fix"] if x["id"] == "ssh_key_add"] == ["root"]


def test_password_fix_on_other_systems():
    # пароль через PAM (keyboard-interactive) выключается вместе с PasswordAuthentication
    s = _people(_ssh(kbd="yes"), [])
    st = {x["id"]: x for x in ssh_audit.advice(s, NOW)[0]["fix"]}
    assert st["ssh_dropin"]["cmd"].splitlines()[1] == (
        "echo 'KbdInteractiveAuthentication no' | sudo tee -a /etc/ssh/sshd_config.d/00-no-password.conf")
    assert "ssh_guard" not in st  # fail2ban уже стоит
    # OpenSSH 7.4 без Include: строка первой в sshd_config, сервис sshd, fail2ban без команды apt
    old = _people(_ssh(guard="", banner="OpenSSH_7.4"), [], os_name="CentOS Linux 7 (Core)")
    st = {x["id"]: x for x in ssh_audit.advice(old, NOW)[0]["fix"]}
    assert "ssh_dropin" not in st and st["ssh_conf"]["cmd"] == (
        "sudo cp -an /etc/ssh/sshd_config /etc/ssh/sshd_config.bak-nopass\n"
        "sudo sed -i '1i PasswordAuthentication no' /etc/ssh/sshd_config")
    assert st["ssh_reload"]["cmd"] == "sudo systemctl reload sshd"
    assert st["ssh_undo"]["cmd"].startswith("sudo cp -a /etc/ssh/sshd_config.bak-nopass /etc/ssh/sshd_config")
    assert "cmd" not in st["ssh_guard"]


def test_password_login_can_be_kept_by_decision():
    s = _people(_ssh(), [], mutes=["ssh:ssh_password"])
    a = ssh_audit.advice(s, NOW)[0]
    assert a["muted"] is True and a["fix"]  # пункт остается в карточке, решение снимается "вернуть"
