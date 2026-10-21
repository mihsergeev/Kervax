"""Признаки взлома в файлах сайтов (app/webfiles.py, helper webfiles-setup): выкладка или правка,
php в загрузках, привязка корня к монитору, проблема и алерт сервера, кнопки."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, setup_scripts, tamper, webfiles
from app.models import Check, Server

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
ROOT = {"key": "2080-11", "where": "wordpress-7d9f", "path": "/var/www/html", "domains": [], "files": 2400,
        "big": False}


def blk(*runs, ts=T, roots=(ROOT,)):
    return {"v": 1, "ts": ts, "roots": list(roots), "runs": list(runs)}


def run(ts, **d):
    return {"ts": ts, "roots": [{"key": ROOT["key"], **d}]}


def test_edit_and_php_outside_deploy():
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    assert st["roots"]["2080-11"]["where"] == "wordpress-7d9f" and "php" not in st["roots"]["2080-11"]
    st = webfiles.process(st, blk(run(T - 600, n_new=1, n_changed=1,
                                      new=[["wp-content/uploads/2026/10/lock360.php", T, 900]],
                                      changed=[["wp-content/themes/shop/js/prod/custom.js", T, 4100]])), NOW)
    r = st["roots"]["2080-11"]
    assert list(r["php"]) == ["wp-content/uploads/2026/10/lock360.php"]
    assert list(r["edit"]) == ["wp-content/themes/shop/js/prod/custom.js"]
    # тот же блок второй раз (агент шлет файл каждую минуту) - ничего не меняет
    assert webfiles.process(st, blk(run(T - 600, n_new=1, new=[["admin.php", T, 10]])), NOW)["roots"] == st["roots"]
    # файл удалили - признак уходит сам
    st = webfiles.process(st, blk(run(T - 300, n_gone=1, gone=[["wp-content/uploads/2026/10/lock360.php", 0, 0]])),
                          NOW)
    assert "php" not in st["roots"]["2080-11"]


def test_deploy_is_not_a_sign_but_php_in_uploads_is():
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    many = [[f"wp-content/plugins/seo/inc/f{i}.php", T, 100] for i in range(9)]
    st = webfiles.process(st, blk(run(T - 600, n_new=12, n_changed=30, new=many + [
        ["wp-content/uploads/cache.php", T, 300], ["wp-content/uploads/index.php", T, 28]],
        changed=[["wp-includes/version.php", T, 900]])), NOW)
    r = st["roots"]["2080-11"]
    assert r["deploy"]["n"] == 42
    assert list(r["php"]) == ["wp-content/uploads/cache.php"]  # index.php-заглушка не в счет
    assert "edit" not in r


def test_owners_by_domain_or_single_site():
    checks = [(1, "www.shop.example.com", {7}), (2, "blog.example.org", {7}), (3, "api.example.net", {9})]
    assert webfiles.owners({"domains": ["shop.example.com"]}, 7, checks) == [1]
    assert webfiles.owners({"domains": ["*.example.net"]}, 7, checks) == [3]
    assert webfiles.owners({"domains": []}, 9, checks) == [3]
    assert webfiles.owners({"domains": []}, 7, checks) == []  # на сервере два сайта - не угадываем


def test_server_problem_and_alert():
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    st = webfiles.process(st, blk(run(T - 600, n_new=2, new=[["lock360.php", T, 900], ["about.php", T, 900]])), NOW)
    st["loose"] = ["2080-11"]
    probs = webfiles.problems(st)
    assert probs == [(2, "похоже на взлом: новые php в /var/www/html (wordpress-7d9f): about.php, lock360.php",
                      "php:2080-11:about.php,lock360.php")]
    assert webfiles.tg_short(probs) == ("Похоже на взлом: новые php", ["about.php, lock360.php",
                                                                       "/var/www/html (wordpress-7d9f)"])
    s = Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
               offline_after_seconds=120, alert_sustain_seconds=900, last_report={}, alert_state={},
               alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
               disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
               disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[],
               webfiles=st)
    lvl, ctx = collector._server_conditions(s, NOW)["webfiles"]
    assert lvl == 3 and ctx["sig"] == ["php:2080-11:about.php,lock360.php"]
    rows = [(p["level"], p.get("section"), p["sec"]) for p in collector.server_problems(s, NOW) if p["kind"] == "webfiles"]
    assert rows == [(2, "services", "files")]
    # одна правка без сайта: желтая строка, в Telegram тихо
    s.webfiles = {"roots": {"k": {"path": "/srv/app", "where": "host", "edit": {"a.js": {"since": "x", "s": 1}}}},
                  "loose": ["k"]}
    assert collector._server_conditions(s, NOW)["webfiles"][0] == 0
    assert [p["level"] for p in collector.server_problems(s, NOW) if p["kind"] == "webfiles"] == [1]
    assert collector._recovery_detail("webfiles", {}, {}) == "новых php в файлах сайтов больше нет"


def test_site_signs_count_with_page_signs():
    st = {"signs": {"edit": {"since": NOW.isoformat(), "files": [
        {"p": "wp-content/themes/shop/js/prod/custom.js", "sid": 1, "root": "k", "where": "web-a, wordpress"}]}}}
    assert tamper.level(st) == "one" and tamper.summary(st) == "правка custom.js вне выкладки"
    st["signs"]["dom"] = {"list": {"evil.example.net": {"src": "/js/custom.js"}}}
    assert tamper.level(st) == "hack"
    row = next(r for r in tamper.view(st)["signs"] if r["key"] == "edit")
    assert row["files"] == ["wp-content/themes/shop/js/prod/custom.js"] and row["where"] == "web-a, wordpress"
    st["signs"]["php"] = {"since": NOW.isoformat(), "files": [{"p": "lock360.php"}, {"p": "8bd31397/about.php"}]}
    assert tamper.alert_text(st).splitlines()[1] == "Новые php: lock360.php, about.php"
    assert tamper.alert_due(st, ["hack"]) == ("tamper", ["hack", "php"])  # новый сильный - новый алерт


def test_helper_needed_and_written_right():
    assert setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "nginx"}]})
    assert setup_scripts.setup_needed("webfiles-setup", {"extras": {"fpm": {"v": 1}}})
    assert not setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "Traefik"}]})
    src = (Path(__file__).resolve().parents[2] / "agent" / "webfiles-setup.sh").read_text(encoding="utf-8")
    assert "\r" not in src and "KERVAX_SETUP_VERSION=0.1" in src and "report.d/webfiles.json" in src
    assert 'mv -f "$HELPER.new" "$HELPER"' in src and "versions/webfiles-setup.ver" in src


async def _seed(client, domains):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    rep = {"extras": {"webfiles": blk(run(T - 1200, first=True), run(T - 600, n_new=1, n_changed=1,
           new=[["wp-content/uploads/lock360.php", T, 900]], changed=[["js/custom.js", T, 100]]),
           roots=({**ROOT, "domains": domains},))}}
    async with f() as s:
        srv = Server(name="web-a", token_hash="x", last_report=rep, external_ip="203.0.113.5")
        a = Check(name="shop", type="http", target="https://shop.example.com")
        b = Check(name="blog", type="http", target="https://blog.example.org")
        s.add_all([srv, a, b])
        await s.commit()
        return f, srv.id, a.id, b.id


async def test_track_maps_root_to_site_and_accept(client, auth_headers, monkeypatch):
    monkeypatch.setattr(webfiles, "STALE", 10 ** 9)
    f, sid, a, b = await _seed(client, ["www.shop.example.com"])
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        ca, cb, srv = await s.get(Check, a), await s.get(Check, b), await s.get(Server, sid)
        assert set(ca.tamper_state["signs"]) == {"php", "edit"} and not cb.tamper_state
        assert not srv.webfiles.get("loose")
    r = await client.post(f"/api/checks/{a}/tamper-accept", json={"key": "php"}, headers=auth_headers)
    assert r.status_code == 200 and [x["key"] for x in r.json()["tamper"]["signs"]] == ["edit"]
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        assert set((await s.get(Check, a)).tamper_state["signs"]) == {"edit"}  # принятый не вернулся
        assert "php" not in (await s.get(Server, sid)).webfiles["roots"]["2080-11"]


async def test_track_loose_root_is_server_problem(client, auth_headers, monkeypatch):
    monkeypatch.setattr(webfiles, "STALE", 10 ** 9)
    f, sid, a, _b = await _seed(client, [])
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        assert (await s.get(Server, sid)).webfiles["loose"] == ["2080-11"]
        assert not (await s.get(Check, a)).tamper_state
    r = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    wf = r.json()["webfiles"]
    assert wf["roots"][0]["php"][0]["p"] == "wp-content/uploads/lock360.php" and wf["roots"][0]["site"] is False
    r = await client.post(f"/api/servers/{sid}/webfiles-accept", json={"root": "2080-11"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["webfiles"]["roots"][0]["php"] == []
    async with f() as s:
        assert webfiles.problems((await s.get(Server, sid)).webfiles) == []


def test_old_roots_drop_after_a_week():
    st = webfiles.process(None, blk(run(T - 600, first=True)), NOW - timedelta(days=8))
    st = webfiles.process(st, blk(roots=()), NOW)
    assert st["roots"] == {}
