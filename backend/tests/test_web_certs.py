"""Сертификаты nginx (helper webserver-setup 0.24): истекает через неделю и меньше или истек."""
from datetime import datetime, timezone
from pathlib import Path

from app import collector, web_certs
from app.models import Server

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
DAY = 86400


def _rep(*certs, renew="timer", ts=T):
    return {"extras": {"web-certs": {"v": 1, "ts": ts, "renew": renew, "certs": list(certs)}}}


def _c(days, file="/etc/letsencrypt/live/shop.example.com/fullchain.pem", where="host",
       names="shop.example.com www.shop.example.com"):
    return {"where": where, "file": file, "end": T + int(days * DAY), "cn": "shop.example.com", "names": names}


def _srv(rep):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_expiry_levels():
    assert web_certs.problems(_rep(_c(60)), NOW) == []
    # за месяц и даже за десять дней - не беда: срок виден в "Сертификатах", алерт - с недели
    assert web_certs.problems(_rep(_c(10.5)), NOW) == []
    assert web_certs.problems(_rep(_c(5.5)), NOW) == [
        (1, "сертификат shop.example.com, www.shop.example.com истекает через 5 дн (на хосте: "
            "/etc/letsencrypt/live/shop.example.com/fullchain.pem)",
         "host:/etc/letsencrypt/live/shop.example.com/fullchain.pem")]
    assert web_certs.problems(_rep(_c(2)), NOW)[0][0] == 1
    assert web_certs.problems(_rep(_c(0.5)), NOW)[0][0] == 2
    expired = web_certs.problems(_rep(_c(-3, file="/certs/api.pem", where="front", names="")), NOW)
    assert expired == [(2, "сертификат shop.example.com истек 3 дн назад (в контейнере front: /certs/api.pem), "
                           "сайты на нем уже отдают ошибку", "front:/certs/api.pem")]
    # продление не настроено - так и пишем; не Let's Encrypt - не пишем
    assert web_certs.problems(_rep(_c(5), renew="none"), NOW)[0][1].endswith(", продление certbot не настроено")
    assert web_certs.problems(_rep(_c(5, file="/etc/ssl/own.pem"), renew="none"), NOW)[0][1].endswith(".pem)")
    assert web_certs.problems(_rep(_c(5), ts=T - 7200), NOW) == []  # helper давно молчит - не судим


def test_alert_home_and_view():
    s = _srv(_rep(_c(5), _c(-1, file="/certs/old.pem", where="front")))
    lvl, ctx = collector._server_conditions(s, NOW)["web_cert"]
    assert lvl == 2 and ctx["sig"] == ["front:/certs/old.pem", "host:/etc/letsencrypt/live/shop.example.com/fullchain.pem"]
    rows = [(p["level"], p.get("section")) for p in collector.server_problems(s, NOW) if p["kind"] == "web_cert"]
    assert rows == [(2, "services"), (1, "services")]
    assert [x["days"] for x in web_certs.view(s.last_report, NOW)] == [-1, 5]
    assert collector._server_conditions(_srv(_rep(_c(60))), NOW)["web_cert"][0] == 0
    assert collector._recovery_detail("web_cert", {}, {}) == "сертификаты веб-серверов снова в сроке"


def test_helper_collects_certs():
    src = (Path(__file__).resolve().parents[2] / "agent" / "webserver-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.30" in src and "collect_certs" in src and "report.d/web-certs.json" in src
    assert "cert_renew" in src and "\r" not in src


def test_helper_renew_sees_snap_certbot():
    """certbot из snap продлевает через snap.certbot.renew.timer - это таймер, а не "продление не
    настроено" (helper 0.25)"""
    import re
    import shutil
    import subprocess

    import pytest

    src = (Path(__file__).resolve().parents[2] / "agent" / "webserver-setup.sh").read_text(encoding="utf-8")
    fn = re.search(r"^cert_renew\(\) \{\n.*?^\}\n", src, re.S | re.M).group(0)
    assert "snap.certbot.renew.timer" in fn and "snap.certbot.renew.service" in fn
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("нет bash")

    def renew(units):
        # systemctl отвечает "да" только для перечисленных "is-failed unit"/"is-active unit", cron пуст
        stub = ("systemctl() { case \" %s \" in *\" $1:$3 \"*) return 0;; esac; return 1; }\n"
                "grep() { return 1; }\n" % " ".join(units))
        out = subprocess.run([bash, "-c", stub + fn + "cert_renew"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip()

    assert renew([]) == "none"
    assert renew(["is-active:certbot.timer"]) == "timer"
    assert renew(["is-active:snap.certbot.renew.timer"]) == "timer"
    assert renew(["is-active:snap.certbot.renew.timer", "is-failed:snap.certbot.renew.service"]) == "failed"
    assert renew(["is-active:certbot.timer", "is-failed:certbot.service"]) == "failed"
