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
    assert ssh_audit.advice(s, NOW) == [{"kind": "ssh_password", "level": 1, "text":
                                         "SSH пускает по паролю, а порт 2221 открыт в интернет: оставьте вход по ключам "
                                         "(PasswordAuthentication no)"}]
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
    assert "KERVAX_SETUP_VERSION=0.4" in src and "ssh_info()" in src and "addr=203.0.113.1" in src
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
                                                     "node": "OpenSSH_9.6p1"}}
    assert ssh_audit.problems(s, NOW) == [] and ssh_audit.advice(s, NOW) == []
    assert open_ports.view(s)["foreign"][0]["ip"] == "203.0.113.5"
    # та же версия (другой пакет поверх) - та же нода
    ex["banners"] = {"203.0.113.5:2221": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19"}
    assert open_ports.foreign(_srv(ssh=_ssh(banner="OpenSSH_9.6p1"), exposure=ex)) == {}
