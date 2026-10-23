"""Конфиг веб-сервера не проходит проверку: сайты упадут при следующем reload."""
from datetime import datetime, timezone
from pathlib import Path

from app import collector, web_config
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def _rep(*items, ts=T):
    return {"extras": {"web-config": {"v": 1, "ts": ts, "items": list(items)}}}


def _srv(rep):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


BAD = {"server": "nginx", "where": "host", "ok": False,
       "err": '[emerg] unknown directive "proxy_pas" in /etc/nginx/conf.d/shop.conf:12'}
BAD_C = {"server": "caddy", "where": "front", "ok": False, "err": "Error: adapting config: unrecognized directive: revers"}


def test_problems():
    assert web_config.problems(_rep({"server": "nginx", "where": "host", "ok": True, "err": ""}), NOW) == []
    assert web_config.problems(_rep(BAD, BAD_C), NOW) == [
        (1, 'конфиг nginx на хосте не проходит проверку: [emerg] unknown directive "proxy_pas" in '
            '/etc/nginx/conf.d/shop.conf:12. Следующий reload или перезапуск уронит сайты', "nginx:host"),
        (1, "конфиг Caddy в контейнере front не проходит проверку: Error: adapting config: unrecognized "
            "directive: revers. Следующий reload или перезапуск уронит сайты", "caddy:front")]
    assert web_config.problems(_rep(BAD, ts=T - 7200), NOW) == []  # helper давно молчит - не судим
    # nginx в контейнере пишет ошибку строкой лога: время и pid срезаем
    logline = {"server": "nginx", "where": "test-nginx", "ok": False, "err": '2026/10/12 20:00:37 [emerg] 56427#56427: '
               'host not found in upstream "app-fpm" in /etc/nginx/conf.d/site.conf:34'}
    assert web_config.problems(_rep(logline), NOW)[0][1] == (
        'конфиг nginx в контейнере test-nginx не проходит проверку: [emerg] host not found in upstream "app-fpm" '
        'in /etc/nginx/conf.d/site.conf:34. Следующий reload или перезапуск уронит сайты')


def test_alert_and_home():
    s = _srv(_rep(BAD, BAD_C))
    lvl, ctx = collector._server_conditions(s, NOW)["web_config"]
    assert lvl == 1 and ctx["sig"] == ["nginx:host", "caddy:front"]
    rows = [(p["level"], p.get("section")) for p in collector.server_problems(s, NOW) if p["kind"] == "web_config"]
    assert rows == [(1, "services"), (1, "services")]
    assert web_config.view(s.last_report, NOW)[0] == {"server": "nginx", "where": "host", "ok": False, "err": BAD["err"]}
    assert collector._server_conditions(_srv(_rep()), NOW)["web_config"][0] == 0
    assert collector._recovery_detail("web_config", {}, {}) == "конфиги веб-серверов снова проходят проверку"


def test_helper_checks_configs():
    src = (Path(__file__).resolve().parents[2] / "agent" / "webserver-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.27" in src and "check_configs()" in src and "report.d/web-config.json" in src
    assert "nginx -t" in src and "caddy validate" in src and "\r" not in src


def test_installers_swap_helpers_in_with_mv():
    """Helper, который запускает таймер, пишется рядом и подменяется mv: перезапись на месте
    (cat >) роняла запуск, шедший в этот момент, - bash дочитывал новый текст с середины строки"""
    import re
    agent = Path(__file__).resolve().parents[2] / "agent"
    for f in sorted(agent.glob("*-setup.sh")):
        src = f.read_text(encoding="utf-8")
        assert not re.search(r'cat > "\$(HELPER|RATE|FIXER|RUN_DUMPS)" <<', src), f.name
    for name in ("dbstat-setup.sh", "webserver-setup.sh", "kubeexpiry-setup.sh"):
        src = (agent / name).read_text(encoding="utf-8")
        assert '"$HELPER" || true' not in src and "systemctl start --no-block" in src, name
    assert '"$RATE" || true' not in (agent / "webserver-setup.sh").read_text(encoding="utf-8")
