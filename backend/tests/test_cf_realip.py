"""Белые списки обходятся подменой CF-Connecting-IP: веб-сервер верит заголовку от любого адреса, а
80/443 открыты в обход Cloudflare (helper webserver-setup 0.27 + внешняя проверка портов)."""
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

from app import cf_realip, collector, open_ports, srv_short
from app.models import Server

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
_NOT_KEYBOARD = re.compile("[—–«»…·≥≤↳→ёЁ ]")

# ingress-nginx в поде: так он рендерит nginx.conf при proxy-real-ip-cidr по умолчанию
INGRESS = {"server": "nginx", "where": "pod default/ingress-nginx-controller-59b4cff54c-dzx64",
           "hdr": ["CF-Connecting-IP", "X-Forwarded-For"], "from": ["0.0.0.0/0"], "ports": [80, 443, 8181],
           "acl": 7, "names": ["grafana.example.com", "portainer.example.com"]}
# nginx фронта в том же кластере верит только Cloudflare и сети подов - так и надо
FRONT = {"server": "nginx", "where": "pod default/front-64c946f85b-62llq", "hdr": ["CF-Connecting-IP"],
         "from": ["173.245.48.0/20", "103.21.244.0/22", "2400:cb00::/32", "10.0.0.0/8"], "ports": [80],
         "acl": 1, "names": ["example.com"]}


def _ex(states: dict[int, str], cf_n: int = 2, ts: datetime = NOW, via: str = "Казахстан") -> dict:
    return {"ts": ts.isoformat(), "via": via, "own": False, "ips": ["203.0.113.5"], "rules": open_ports.RULES,
            "via_ips": ["198.51.100.9"],
            "ports": [{"ip": "203.0.113.5", "port": p, "state": st, "what": "сервис Kubernetes default/ingress (LoadBalancer)",
                       **open_ports.label(p, "")} for p, st in states.items()],
            "cf": {"sites": ["grafana.example.com"], "n": cf_n, "checked": 2, "total": 2}}


def _srv(items, exposure, mutes=None, rep_extra=None, ts=NOW):
    rep = {"extras": {"web-realip": {"v": 1, "ts": int(ts.timestamp()), "items": items}}, **(rep_extra or {})}
    return Server(id=1, name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep, alert_state={},
                  alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
                  disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
                  disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[],
                  external_ip="", local_ip="", exposure=exposure)


OPEN = {80: "open", 443: "open", 22: "open"}
GUARDED = {80: "filtered", 443: "filtered", 22: "open"}


def test_trust_in_anyone_with_ports_past_cloudflare():
    s = _srv([INGRESS, FRONT], _ex(OPEN))
    got = cf_realip.findings(s, NOW)
    assert [f["key"] for f in got] == ["nginx:pod default/ingress-nginx-controller"]
    f = got[0]
    assert f["label"] == "ingress-nginx" and f["ports"] == [80, 443] and f["hdr"] == "CF-Connecting-IP"
    assert f["trust"] == "set_real_ip_from 0.0.0.0/0"
    assert cf_realip.problems(s, NOW) == [(2, "белые списки обходятся подменой CF-Connecting-IP: ingress-nginx в поде "
                                              "default/ingress-nginx-controller верит заголовку от любого адреса "
                                              "(set_real_ip_from 0.0.0.0/0), а 80 и 443 открыты в обход Cloudflare",
                                           "nginx:pod default/ingress-nginx-controller")]


def test_filter_that_lets_only_cloudflare_closes_it():
    """nftables, ufw или NetworkPolicy пускают на 80/443 только Cloudflare: внешняя проверка видит их
    закрытыми, и признака нет, хотя ingress все так же верит всем"""
    assert cf_realip.findings(_srv([INGRESS], _ex(GUARDED)), NOW) == []
    # Cilium с kube-proxy-replacement: ufw и nftables ничего не закрывают, правила хоста пускают
    # 80/443 всем, а NetworkPolicy пускает только Cloudflare. Судим по проверке снаружи, не по правилам
    s = _srv([INGRESS], _ex(GUARDED))
    s.last_report["extras"]["changes"] = {"fw": {"p4": "ACCEPT", "p6": "ACCEPT", "in": ["46 * 80,443"], "fwd": []}}
    assert cf_realip.findings(s, NOW) == []
    s.exposure = _ex(OPEN)  # а открытый снаружи - красный, что бы ни было в правилах
    assert cf_realip.findings(s, NOW)
    # открыт один 80 - признак про него одного
    assert cf_realip.findings(_srv([INGRESS], _ex({80: "open", 443: "filtered"})), NOW)[0]["ports"] == [80]


def test_needs_cloudflare_a_fresh_outside_check_and_the_helper():
    assert cf_realip.findings(_srv([INGRESS], _ex(OPEN, cf_n=0)), NOW) == []  # сайты не через Cloudflare
    assert cf_realip.findings(_srv([INGRESS], _ex(OPEN, ts=NOW - timedelta(days=3))), NOW) == []
    assert cf_realip.findings(_srv([INGRESS], _ex(OPEN, via="панель") | {"own": True}), NOW) == []
    assert cf_realip.findings(_srv([INGRESS], _ex(OPEN), ts=NOW - timedelta(hours=2)), NOW) == []
    assert cf_realip.findings(_srv([INGRESS], None), NOW) == []
    # порт открыт проверке только по белому списку с ее адресом - для интернета закрыт
    s = _srv([dict(INGRESS, where="host")], _ex(OPEN))
    assert cf_realip.findings(s, NOW)
    s.last_report["extras"]["changes"] = {"fw": {"p4": "DROP", "in": ["4 198.51.100.9/32 80,443"], "fwd": []}}
    assert cf_realip.findings(s, NOW) == []
    # k3s с klipper: на 80/443 никто на хосте не слушает, svclb заворачивает их DNAT в под мимо INPUT,
    # и белый список ufw с адресом проверки к ним отношения не имеет - открыто для всех
    s.last_report["listen"] = [{"p": "tcp", "ip": "0.0.0.0", "port": 22}]
    assert cf_realip.findings(s, NOW)
    s.last_report["listen"].append({"p": "tcp", "ip": "0.0.0.0", "port": 443})
    s.last_report["listen"].append({"p": "tcp6", "ip": "::", "port": 80})
    assert cf_realip.findings(s, NOW) == []  # nginx на хосте: INPUT и правда решает


def test_without_allow_deny_the_client_ip_is_still_forged():
    """Белых списков нет, а адрес клиента подделывается все равно: в логах, банах fail2ban, лимитах входа
    WordPress. Желтое в "Что сломано" и в карточке, без сообщения в Telegram"""
    s = _srv([dict(INGRESS, acl=0, names=[])], _ex(OPEN))
    got = cf_realip.findings(s, NOW)
    assert [cf_realip.level(f) for f in got] == [1]
    assert cf_realip.problems(s, NOW) == [(1, "IP клиента можно подделать заголовком CF-Connecting-IP: ingress-nginx в поде "
                                              "default/ingress-nginx-controller верит заголовку от любого адреса "
                                              "(set_real_ip_from 0.0.0.0/0), а 80 и 443 открыты в обход Cloudflare - "
                                              "баны, лимиты входа и логи видят поддельный адрес",
                                           "nginx:pod default/ingress-nginx-controller")]
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "cf_realip"]
    assert len(rows) == 1 and rows[0]["level"] == 1
    lvl, _ctx = collector._server_conditions(s, NOW)["cf_realip"]
    assert lvl == 0
    for line in [r[1] for r in cf_realip.problems(s, NOW)]:
        assert not _NOT_KEYBOARD.search(line), line
    # рядом второй веб-сервер с allow/deny: алерт про него одного
    s = _srv([dict(INGRESS, acl=0), dict(INGRESS, where="host", acl=2)], _ex(OPEN))
    lvl, ctx = collector._server_conditions(s, NOW)["cf_realip"]
    assert lvl == 2 and ctx["sig"] == ["nginx:host"]


def test_what_counts_as_trust_in_anyone():
    t = cf_realip.trust_any
    assert t({"server": "nginx", "from": ["::/0"]}) == "set_real_ip_from ::/0"
    assert t({"server": "nginx", "from": ["0.0.0.0/1"]}) == "set_real_ip_from 0.0.0.0/1"
    # Cloudflare, частные сети и отдельный адрес своего балансировщика - доверие ограничено
    assert t(FRONT) == ""
    assert t({"server": "nginx", "where": "web", "from": ["172.16.0.0/12", "203.0.113.10"]}) == ""
    assert t({"server": "traefik", "from": ["*"]}) == "forwardedHeaders.insecure"
    assert t({"server": "traefik", "from": ["0.0.0.0/0"]}) == "trustedIPs 0.0.0.0/0"
    assert t({"server": "caddy", "where": "host", "from": ["*"]}) == "remote_ip forwarded"
    # Caddy в контейнере с private_ranges: IPv6 доходит до него с адреса шлюза моста
    priv = ["192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "127.0.0.1/8", "fd00::/8", "::1"]
    assert t({"server": "caddy", "where": "caddy", "from": priv}) == "trusted_proxies private_ranges"
    assert t({"server": "caddy", "where": "host", "from": priv}) == ""


def test_docker_container_by_its_published_ports():
    """nginx в контейнере слушает 8080, наружу опубликован как 443: судим по опубликованному"""
    item = {"server": "nginx", "where": "edge", "hdr": ["X-Forwarded-For"], "from": ["0.0.0.0/0"], "ports": [8080],
            "acl": 1, "names": []}
    docker = {"docker": {"containers": [{"name": "edge", "ports": [{"pub": 443, "priv": 8080, "proto": "tcp"}]}]}}
    s = _srv([item], _ex(OPEN), rep_extra=docker)
    got = cf_realip.findings(s, NOW)
    assert got[0]["ports"] == [443] and got[0]["where"] == "в контейнере edge"
    # опубликован только на 8081 - к 80/443 не относится
    docker["docker"]["containers"][0]["ports"] = [{"pub": 8081, "priv": 8080}]
    assert cf_realip.findings(_srv([item], _ex(OPEN), rep_extra=docker), NOW) == []


def test_decision_survives_a_new_pod_name():
    s = _srv([INGRESS], _ex(OPEN), mutes=["cfip:nginx:pod default/ingress-nginx-controller"])
    assert cf_realip.problems(s, NOW) == []
    s.last_report["extras"]["web-realip"]["items"] = [dict(INGRESS, where="pod default/ingress-nginx-controller-7d9f8b6c5-x2x2x")]
    assert cf_realip.problems(s, NOW) == []
    # в карточке видно с отметкой
    assert [f["decided"] for f in cf_realip.view(s, NOW)] == [True]


def test_replicas_of_one_workload_are_one_row():
    """ingress-nginx с replicaCount 3: три пода с одним конфигом - одна строка с числом подов в
    "Что сломано", карточке и алерте, решение "так задумано" на ворклоаде"""
    pods = [dict(INGRESS, where=f"pod default/ingress-nginx-controller-59b4cff54c-{n}")
            for n in ("dzx64", "k2v9q", "x7rtw")]
    pods[2] = dict(pods[2], acl=8, names=["rabbitmq.example.com"])
    s = _srv([*pods, FRONT], _ex(OPEN))
    got = cf_realip.findings(s, NOW)
    assert len(got) == 1
    f = got[0]
    assert f["key"] == "nginx:pod default/ingress-nginx-controller" and f["pods"] == 3
    assert f["acl"] == 8 and f["names"] == ["grafana.example.com", "portainer.example.com", "rabbitmq.example.com"]
    assert cf_realip.text(f) == ("белые списки обходятся подменой CF-Connecting-IP: ingress-nginx в поде "
                                 "default/ingress-nginx-controller (подов: 3) верит заголовку от любого адреса "
                                 "(set_real_ip_from 0.0.0.0/0), а 80 и 443 открыты в обход Cloudflare")
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "cf_realip"]
    assert len(rows) == 1 and "(подов: 3)" in rows[0]["text"]
    lvl, ctx = collector._server_conditions(s, NOW)["cf_realip"]
    assert lvl == 2 and ctx["sig"] == ["nginx:pod default/ingress-nginx-controller"] and ctx["detail"].count("подмен") == 1
    what, facts = srv_short.short("cf_realip", ctx, s, ctx["detail"], 2, NOW)
    assert facts == ["default/ingress-nginx-controller (подов: 3): set_real_ip_from 0.0.0.0/0",
                     "80 и 443 открыты в обход Cloudflare"]
    for line in [what, *facts]:
        assert not _NOT_KEYBOARD.search(line) and len(line) <= 75, line
    # решение на ворклоаде: поды пересоздали с другими именами - его не теряем
    s.alert_mutes = ["cfip:nginx:pod default/ingress-nginx-controller"]
    assert cf_realip.problems(s, NOW) == []
    s.last_report["extras"]["web-realip"]["items"] = [
        dict(INGRESS, where=f"pod default/ingress-nginx-controller-7d9f8b6c5-{n}") for n in ("bcdfg", "hjklm")]
    assert cf_realip.problems(s, NOW) == []
    assert [(f["decided"], f["pods"]) for f in cf_realip.view(s, NOW)] == [(True, 2)]


def test_owner_from_the_agent_when_the_name_says_nothing():
    """Под DaemonSet с коротким именем: владельца берем из ctrl агента, а не угадываем по суффиксу"""
    items = [dict(INGRESS, where=f"pod edge/{n}") for n in ("edge-proxy-a1", "edge-proxy-b2")]
    pods = [{"ns": "edge", "name": n, "ctrl": "daemonset/edge-proxy"} for n in ("edge-proxy-a1", "edge-proxy-b2")]
    s = _srv(items, _ex(OPEN), rep_extra={"kube": {"pods": pods}})
    got = cf_realip.findings(s, NOW)
    assert [(f["key"], f["where"], f["pods"]) for f in got] == [("nginx:pod edge/edge-proxy", "в поде edge/edge-proxy", 2)]
    # на хосте и в контейнере подов нет - и счетчика в тексте тоже
    host = cf_realip.findings(_srv([dict(INGRESS, where="host")], _ex(OPEN)), NOW)[0]
    assert host["pods"] == 0 and "подов" not in cf_realip.text(host)


def test_alert_and_home_row():
    s = _srv([INGRESS], _ex(OPEN))
    lvl, ctx = collector._server_conditions(s, NOW)["cf_realip"]
    assert lvl == 2 and ctx["sig"] == ["nginx:pod default/ingress-nginx-controller"]
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "cf_realip"]
    assert rows and rows[0]["level"] == 2 and rows[0]["sec"] == "exposure" and rows[0]["mute"] == "cf_realip"
    lvl, _ctx = collector._server_conditions(_srv([INGRESS], _ex(GUARDED)), NOW)["cf_realip"]
    assert lvl == 0


def test_tg_short_format_a():
    s = _srv([INGRESS], _ex(OPEN))
    _lvl, ctx = collector._server_conditions(s, NOW)["cf_realip"]
    what, facts = srv_short.short("cf_realip", ctx, s, ctx["detail"], 2, NOW)
    assert what == "Белые списки обходятся подменой CF-Connecting-IP"
    assert facts == ["default/ingress-nginx-controller: set_real_ip_from 0.0.0.0/0", "80 и 443 открыты в обход Cloudflare"]
    for line in [what, *facts]:
        assert not _NOT_KEYBOARD.search(line) and len(line) <= 75, line
    assert srv_short.icon("cf_realip", 2) == "🔴🎭"
    host = {"label": "nginx", "where": "на хосте", "hdr": "X-Forwarded-For", "trust": "set_real_ip_from 0.0.0.0/0",
            "ports": [443]}
    assert cf_realip.tg_short([host]) == ("Белые списки обходятся подменой X-Forwarded-For",
                                          ["nginx: set_real_ip_from 0.0.0.0/0", "443 открыт в обход Cloudflare"])


def test_helper_collects_trust():
    src = (Path(__file__).resolve().parents[2] / "agent" / "webserver-setup.sh").read_text(encoding="utf-8")
    # пробел перед комментарием: иначе bash взял бы "0.27#" значением, а хвост выполнил командой
    assert "KERVAX_SETUP_VERSION=0.28  # " in src and "\r" not in src
    for part in ("extract_realip()", "caddy_realip()", "collect_realip_traefik()", "report.d/web-realip.json"):
        assert part in src, part
    assert collector._recovery_detail("cf_realip", {}, {}).startswith("белые списки больше не обходятся")
