"""Короткие алерты в Telegram (формат A), группа безопасность и сеть: что открыто в интернет, новые
порты, изменения доступа, второй root, SSH, docker-сокет и контейнеры с ключами от хоста, пропавшие
порты, связь между серверами, трафик тарифа. Тексты - как в прототипе kervax-design."""
import re
from datetime import datetime, timedelta, timezone

from app import (access_audit, changes, docker_exposure, docker_risk, listen_watch, netlinks, open_ports,
                 ssh_audit, traffic)
from app.models import Server

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
_NOT_KEYBOARD = re.compile("[—–«»…·≥≤↳→ёЁ ]")


def _ok(out):
    """Формат A: (что, 0-2 строки), только символы с клавиатуры, строки не длиннее ~70."""
    assert out is not None
    what, facts = out
    assert what and what[0].isupper(), what
    assert len(facts) <= 2
    for s in [what, *facts]:
        assert not _NOT_KEYBOARD.search(s), s
        assert len(s) <= 75, s
    return out


def _srv(**kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, last_seen=NOW, last_report={},
                alert_state={}, alert_mutes=[], external_ip="", local_ip="")
    base.update(kw)
    return Server(**base)


# ---- open_ports: exposed и port_new ----

def _row(port, what, ip="203.0.113.10"):
    return {"ip": ip, "port": port, "state": "open", "what": what, **open_ports.label(port, what)}


MQ_HOST = [_row(4369, "процесс epmd, пользователь root"), _row(5672, "процесс beam.smp, пользователь rabbitmq"),
        _row(25672, "процесс beam.smp, пользователь rabbitmq"),
        _row(10050, "процесс zabbix_agent2, пользователь zabbix"),
        _row(49100, "контейнер node-exporter (prom/node-exporter:v1.9.1)")]


def test_exposed_short():
    # разные сервисы первыми: второй порт RabbitMQ уходит в "и еще"
    assert _ok(open_ports.tg_short_exposed(MQ_HOST)) == (
        "Открыто в интернет", ["epmd 4369, RabbitMQ 5672, Zabbix 10050 и еще 2"])
    # один порт на двух адресах - одна запись; безымянный - по процессу или "порт"
    two = [_row(21, "процесс пользователя root"), _row(21, "процесс пользователя root", "203.0.113.5"),
           _row(4240, "процесс calico-node")]
    assert _ok(open_ports.tg_short_exposed(two)) == ("Открыто в интернет", ["FTP 21, calico-node 4240"])
    assert open_ports.tg_short_exposed([]) is None


def test_exposed_short_from_the_real_check():
    rows = [_row(22, "процесс sshd, пользователь root", "203.0.113.5"),
            _row(6379, "контейнер cache (redis:7)", "203.0.113.5"),
            _row(9000, "контейнер s3 (minio/minio)", "203.0.113.5")]
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"], "ports": rows}
    got = open_ports.exposed(_srv(exposure=ex), NOW)
    assert _ok(open_ports.tg_short_exposed(got)) == ("Открыто в интернет", ["Redis 6379, s3 9000"])


def test_new_ports_short():
    rows = [_row(64100, "контейнер rest-server (restic/rest-server:latest)", "203.0.113.20"),
            _row(64101, "контейнер kervax-rest-tls (restic/rest-server:0.14.0)", "203.0.113.20")]
    assert _ok(open_ports.tg_short_new(rows)) == (
        "Открылись новые порты", ["64100 rest-server, 64101 kervax-rest-tls"])
    assert _ok(open_ports.tg_short_new([_row(2221, "процесс sshd, пользователь root")])) == (
        "Открылся новый порт", ["2221 SSH"])
    assert open_ports.tg_short_new([]) is None


def test_new_ports_short_from_the_real_check():
    new = [_row(8080, "процесс пользователя www-data", "203.0.113.5"),
           _row(6379, "контейнер cache (redis:7)", "203.0.113.5")]
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"],
          "ports": new, "new": new, "new_ts": (NOW - timedelta(hours=1)).isoformat()}
    got = open_ports.new_ports(_srv(exposure=ex), NOW)
    # опасное первым, без имени - только номер
    assert _ok(open_ports.tg_short_new(got)) == ("Открылись новые порты", ["6379 Redis, 8080"])


# ---- changes: access_change ----

def _ev(text, kind="key"):
    return {"kind": kind, "act": "add", "alert": True, "text": text, "ts": NOW.isoformat()}


KEY_ORANGE = _ev("добавлен SSH-ключ пользователю agent_orange: ED25519 SHA256:nI15gi1/FoHgbmt191sAWANCwHMtZBzNsnSIRuJ"
                 "diM4 (agent_orange@shop)")
KEY_MSDEV = _ev("добавлен SSH-ключ пользователю ms-dev: ED25519 SHA256:wrnYGgfnTXVB1XppFv3Ppmk/kDdt9GF2b0LSguLm5lY "
                "(tg-archive deploy mon-a)")


def test_access_change_new_sudo_user():
    ev = [KEY_ORANGE, _ev("новый пользователь agent_orange (uid 1002, /bin/bash)", "user"),
          _ev("agent_orange добавлен в группу sudo", "sudo"), _ev("добавлен /etc/sudoers.d/agent_orange", "sudo")]
    # все про одного пользователя - одна строка, без отпечатков
    assert _ok(changes.tg_short(ev)) == ("Новый пользователь с sudo",
                                         ["agent_orange, вход по ключу agent_orange@shop"])


def test_access_change_key():
    assert _ok(changes.tg_short([KEY_MSDEV])) == ("Добавлен SSH-ключ", ["ms-dev: tg-archive deploy mon-a"])
    two = [KEY_MSDEV, _ev("добавлен SSH-ключ пользователю root: RSA SHA256:bbbb")]
    assert _ok(changes.tg_short(two)) == ("Добавлены SSH-ключи", ["ms-dev: tg-archive deploy mon-a, root"])
    assert changes.tg_short([]) is None


def test_access_change_worst_first_rest_by_name():
    ev = [KEY_MSDEV, _ev("пользователь svc получил uid 0", "user"), _ev("deploy добавлен в группу sudo", "sudo"),
          _ev("добавлен /etc/sudoers.d/90-cloud-init-users", "sudo")]
    assert _ok(changes.tg_short(ev)) == (
        "Пользователь получил uid 0", ["svc", "Еще: ключ ms-dev, sudo у deploy, /etc/sudoers.d/90-cloud-init-users"])
    login = _ev("вход по паролю: root с 198.51.100.9, раньше с этого адреса не входил", "login")
    assert _ok(changes.tg_short([KEY_MSDEV, login])) == (
        "Вход по паролю с нового адреса", ["root с 198.51.100.9", "Еще: ключ ms-dev"])
    # sudoers.d не по имени нового пользователя - это файл, а не "выдали sudo"
    assert _ok(changes.tg_short([_ev("изменен /etc/sudoers", "sudo")])) == (
        "Изменились права sudo", ["/etc/sudoers"])
    assert _ok(changes.tg_short([_ev("что-то новое", "x")])) == ("Изменился доступ", [])


def test_access_change_from_the_real_feed():
    snap = {"v": 1, "ts": int(NOW.timestamp()) - 1200, "keys": [], "users": [{"n": "root", "uid": 0, "sh": "/bin/bash"}],
            "sudo": {"groups": {"sudo": []}, "files": {}}}
    st = changes.apply_snapshot(None, snap, NOW)
    key = {"u": "deploy", "t": "ED25519", "fp": "SHA256:dddd", "c": "ci@gitlab"}
    snap2 = dict(snap, ts=snap["ts"] + 600, keys=[key],
                 users=snap["users"] + [{"n": "deploy", "uid": 1001, "sh": "/bin/bash"}])
    st = changes.apply_snapshot(st, snap2, NOW)
    ev = changes.alert_events(_srv(changes=st), None)
    assert _ok(changes.tg_short(ev)) == ("Новый пользователь", ["deploy, вход по ключу ci@gitlab"])


# ---- access_audit: access_risk ----

def test_access_risk_short():
    items = [(2, "учетка backup с uid 0 - это второй root"), (2, "у учетки test пустой пароль: войти можно без пароля")]
    assert _ok(access_audit.tg_short(items)) == ("Второй root", ["uid 0 у backup, пустой пароль у test"])
    assert _ok(access_audit.tg_short(items[1:])) == ("Пустой пароль у учетки", ["test"])
    assert access_audit.tg_short([]) is None


def test_access_risk_short_from_the_snapshot():
    snap = {"v": 1, "ts": 1, "users": [{"n": "root", "uid": 0}, {"n": "toor", "uid": 0}, {"n": "a", "uid": 1, "pw": "NP"},
                                       {"n": "b", "uid": 2, "pw": "NP"}]}
    ar = access_audit.problems(_srv(last_report={"extras": {"changes": snap}}))
    assert _ok(access_audit.tg_short(ar)) == ("Второй root", ["uid 0 у toor, пустой пароль у a, b"])


# ---- ssh_audit: ssh_risk ----

def test_ssh_risk_short():
    brute = (1, "идет перебор паролей SSH: 273 неудачных входов за 10 минут (больше всех с 198.51.100.9: 120), "
                "вход по паролю включен, а fail2ban и подобного нет")
    assert _ok(ssh_audit.tg_short([brute])) == ("Перебор паролей SSH", ["вход по паролю открыт, fail2ban нет"])
    root = (2, "root может войти по SSH паролем, а порт 22 открыт в интернет")
    assert _ok(ssh_audit.tg_short([root, brute])) == (
        "SSH пускает root по паролю", ["порт 22 открыт в интернет", "Идет перебор паролей, fail2ban нет"])
    root2 = (2, "root может войти по SSH паролем, а порт 22, 2221 открыт в интернет")
    assert _ok(ssh_audit.tg_short([root2]))[1] == ["порты 22, 2221 открыты в интернет"]
    assert ssh_audit.tg_short([]) is None


# ---- docker_exposure: docker_sock ----

def test_docker_sock_short():
    one = [{"name": "caddy-proxy-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.9", "kind": "caddy"}]
    assert _ok(docker_exposure.tg_short(one)) == ("Внешний прокси держит docker-сокет", ["caddy-proxy-caddy-1"])
    many = [{"name": f"edge-{i}", "kind": "traefik"} for i in range(5)]
    assert _ok(docker_exposure.tg_short(many)) == (
        "Внешние прокси держат docker-сокет", ["edge-0, edge-1, edge-2 и еще 2"])
    assert docker_exposure.tg_short([]) is None


# ---- docker_risk ----

def _tc(n):
    return {"name": f"teamcity-teamcity-agent-{n}-1", "image": "jetbrains/teamcity-agent:2023.11.1", "state": "running",
            "priv": True, "compose": {"project": "teamcity", "service": f"teamcity-agent-{n}", "dir": "/opt/tc",
                                      "files": "/opt/tc/docker-compose.yml"}}


def test_docker_risk_short_from_advice():
    rep = {"agent_version": "2.41", "docker": {"access": True, "containers": [_tc(2), _tc(3), _tc(1)]}}
    dr = docker_risk.advice(_srv(last_report=rep))
    assert [x["svc"] for x in dr] == ["teamcity-agent-2", "teamcity-agent-3", "teamcity-agent-1"]
    assert _ok(docker_risk.tg_short(dr)) == ("Контейнеры с root на хосте", ["teamcity-agent 1, 2, 3 (privileged)"])


def test_docker_risk_short_mixed():
    one = [{"name": "angry_austin", "text": "privileged", "svc": ""}]
    assert _ok(docker_risk.tg_short(one)) == ("Контейнер с root на хосте", ["angry_austin (privileged)"])
    # без меток compose (старый агент) - по имени контейнера; одинаковый сервис в разных проектах - тоже
    mixed = [{"name": "a-app-1", "text": "privileged", "svc": "app"}, {"name": "b-app-1", "text": "privileged", "svc": "app"},
             {"name": "web", "text": "docker-сокет"}, {"name": "x", "text": "cap SYS_ADMIN"}]
    assert _ok(docker_risk.tg_short(mixed)) == (
        "Контейнеры с root на хосте", ["a-app-1, b-app-1 (privileged), web (docker-сокет) и еще 1"])
    assert docker_risk.tg_short([]) is None


# ---- listen_watch: listen_gone ----

def test_listen_gone_short():
    one = [{"key": "tcp:5432", "port": "5432", "proc": "postgres", "text": ""}]
    assert _ok(listen_watch.tg_short(one)) == ("Пропал порт 5432", ["Слушал: postgres"])
    many = [{"key": "tcp:5432", "port": "5432", "proc": "postgres"}, {"key": "tcp:6379", "port": "6379", "proc": ""},
            {"key": "tcp:8080", "port": "8080", "proc": "java"}]
    assert _ok(listen_watch.tg_short(many)) == ("Пропали порты 5432, 6379, 8080", ["Слушали: postgres, java"])
    assert _ok(listen_watch.tg_short([{"port": "9000", "proc": ""}])) == ("Пропал порт 9000", [])
    assert listen_watch.tg_short([]) is None


def test_listen_gone_short_from_history():
    hist = {"tcp:5432": {"first": (NOW - timedelta(days=30)).isoformat(), "last": (NOW - timedelta(hours=1)).isoformat(),
                         "proc": "postgres", "miss": (NOW - timedelta(hours=1)).isoformat()}}
    lg = listen_watch.problems(_srv(alert_state={"listen_hist": hist}), NOW)
    assert _ok(listen_watch.tg_short(lg)) == ("Пропал порт 5432", ["Слушал: postgres"])


# ---- netlinks: link ----

def test_link_short():
    st = [{"id": 1, "name": "vpn-b:443 (выход каскада)", "host": "vpn-b", "port": 443,
           "note": "выход каскада", "loss": 100.0, "err": "timeout", "held": False}]
    assert _ok(netlinks.tg_short(st)) == ("Нет связи с vpn-b", ["выход каскада"])
    # отказ - понятной причиной; связь с теми же потерями, но с другим сервером - в "Еще"
    st2 = [{"id": 2, "name": "db:5432", "host": "db", "port": 5432, "note": "", "loss": 25.0, "err": "", "held": False},
           dict(st[0], err="connection refused")]
    assert _ok(netlinks.tg_short(st2)) == (
        "Нет связи с vpn-b", ["выход каскада, порт не принимает подключения", "Еще: db"])
    assert _ok(netlinks.tg_short(st2[:1])) == ("Связь с db теряет 25% подключений", [])


def test_link_short_old_state_and_same_host():
    # сводка до полей host/port: разбор name; две связи с одним сервером - с портами
    st = [{"id": 1, "name": "vpn-out:443 (выход)", "loss": 100.0, "err": "timeout", "held": False},
          {"id": 2, "name": "vpn-out:22", "loss": 100.0, "err": "no route to host", "held": False},
          {"id": 3, "name": "kz:443", "loss": 100.0, "err": "timeout", "held": True}]
    assert _ok(netlinks.tg_short(st)) == ("Нет связи с vpn-out:443", ["выход", "Еще: vpn-out:22"])
    assert netlinks.tg_short(st[2:]) is None and netlinks.tg_short(None) is None


def test_link_short_from_problems():
    """srv_short зовет tg_short(ctx["items"]) - вывод problems(): адрес и причина из текста,
    со сводкой вторым аргументом - из ее полей."""
    st = [{"id": 1, "name": "kz-exit:443 (выход каскада)", "host": "kz-exit", "port": 443, "note": "выход каскада",
           "loss": 100.0, "err": "connection refused", "held": False},
          {"id": 2, "name": "db:5432", "loss": 40.0, "err": "timeout", "held": False}]
    lk = netlinks.problems(st)
    want = ("Нет связи с kz-exit", ["выход каскада, порт не принимает подключения", "Еще: db"])
    assert _ok(netlinks.tg_short(lk)) == want
    assert _ok(netlinks.tg_short(lk, st)) == want
    assert _ok(netlinks.tg_short(lk[1:])) == ("Связь с db теряет 40% подключений", [])
    assert netlinks.tg_short([]) is None


# ---- traffic ----

def _ts(used, limit_tb=20, forecast=None, at=NOW):
    return {"at": at.isoformat(), "used": used, "limit": limit_tb * 10**12, "end": "2026-10-31", "count": "out",
            "forecast": forecast}


def test_traffic_short():
    assert _ok(traffic.tg_short(_ts(18.2e12), NOW)) == ("Кончается трафик тарифа", ["18 из 20тб, до сброса 9 дней"])
    assert _ok(traffic.tg_short(_ts(20.4e12), NOW)) == ("Кончился трафик тарифа", ["20 из 20тб, до сброса 9 дней"])
    assert _ok(traffic.tg_short(_ts(950e9, 1), NOW)) == ("Кончается трафик тарифа", ["950гб из 1тб, до сброса 9 дней"])
    assert _ok(traffic.tg_short(_ts(12e12, forecast=24.3e12), NOW)) == (
        "Трафика тарифа не хватит до сброса", ["уже 12 из 20тб, к сбросу будет около 24тб"])
    # до сброса меньше двух суток - часами
    late = datetime(2026, 10, 30, 6, tzinfo=timezone.utc)
    assert traffic.tg_short(_ts(19e12, at=late), late)[1] == ["19 из 20тб, до сброса 18 ч"]


def test_traffic_short_nothing():
    assert traffic.tg_short(_ts(5e12), NOW) is None
    assert traffic.tg_short(_ts(19e12, at=NOW - timedelta(hours=3)), NOW) is None  # сводка протухла
    assert traffic.tg_short({"used": 1, "limit": 0}, NOW) is None
    assert traffic.tg_short(None, NOW) is None
    # одним аргументом, как зовет srv_short: время - текущее
    fresh = _ts(19e12, at=datetime.now(timezone.utc))
    fresh["end"] = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
    assert _ok(traffic.tg_short(fresh))[0] == "Кончается трафик тарифа"
