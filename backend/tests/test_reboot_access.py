"""Не переживет перезагрузку (контейнеры, юниты) и доступы (заброшенные учетки, слабые ключи, второй root)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import access_audit, collector, reboot_survive
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
    rep = _units({"unit": "myapp.service", "started": TS - 3 * DAY}, {"unit": "debug.service", "started": TS - 600})
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


def test_helpers_report_the_new_fields():
    ch = (AGENT / "changes-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.6" in ch and '"seen_since":%s' in ch and '\\"pw\\":' in ch and '"nopasswd":[%s]' in ch
    un = (AGENT / "units-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.4" in un and '"noboot":[%s]' in un
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
