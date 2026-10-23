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
                      "php:wordpress-7d9f:/var/www/html:about.php,lock360.php")]
    assert webfiles.tg_short(probs) == ("Похоже на взлом: новые php", ["about.php, lock360.php",
                                                                       "/var/www/html (wordpress-7d9f)"])
    s = Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
               offline_after_seconds=120, alert_sustain_seconds=900, last_report={}, alert_state={},
               alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
               disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
               disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[],
               webfiles=st)
    lvl, ctx = collector._server_conditions(s, NOW)["webfiles"]
    assert lvl == 3 and ctx["sig"] == ["php:wordpress-7d9f:/var/www/html:about.php,lock360.php"]
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
    assert tamper.level(st) == "one" and tamper.summary(st) == "правка custom.js не через git/деплой"
    st["signs"]["dom"] = {"list": {"evil.example.net": {"src": "/js/custom.js"}}}
    assert tamper.level(st) == "hack"
    row = next(r for r in tamper.view(st)["signs"] if r["key"] == "edit")
    assert row["files"] == ["wp-content/themes/shop/js/prod/custom.js"] and row["where"] == "web-a, wordpress"
    st["signs"]["php"] = {"since": NOW.isoformat(), "files": [{"p": "lock360.php"}, {"p": "8bd31397/about.php"}]}
    assert tamper.alert_text(st).splitlines()[1] == "Новые php: lock360.php, about.php"
    assert tamper.alert_due(st, ["hack"]) == ("tamper", ["hack", "php"])  # новый сильный - новый алерт


def test_helper_needed_and_written_right():
    assert setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "nginx"}]})
    assert setup_scripts.setup_needed("webfiles-setup", {"extras": {"fpm": {"v": 1, "pools": [{"pool": "www"}]}}})
    # fpm-setup стоит всюду: без PHP его пустой блок не повод, на ноде условие скрипта ложно
    assert not setup_scripts.setup_needed("webfiles-setup", {"extras": {"fpm": {"v": 1, "pools": [], "skipped": []}}})
    assert not setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "Traefik"}]})
    src = (Path(__file__).resolve().parents[2] / "agent" / "webfiles-setup.sh").read_text(encoding="utf-8")
    assert "\r" not in src and "KERVAX_SETUP_VERSION=0.5" in src and "report.d/webfiles.json" in src
    assert 'mv -f "$HELPER.new" "$HELPER"' in src and "versions/webfiles-setup.ver" in src
    # контейнер: только верхний слой overlay, ключ по контейнеру, старт - не правка, ассеты мимо
    assert "upperdir=" in src and 'key="c${cid:0:12}' in src and "START_WIN=600" in src
    assert "! -path '*/web/assets/*'" in src
    # выкладка через git: reflog читается файлом, сам git не запускается (конфиг репозитория мог бы
    # запустить фильтр от root), корень в bind-mount ищется на хосте через mountinfo
    assert "/logs/HEAD" in src and "/proc/1/mountinfo" in src and '\\"git\\":[$GEV]' in src
    assert "git diff" not in src and "git -C" not in src and "git status" not in src and "hash-object" not in src
    # сверка с HEAD: от git только ls-tree (без хуков, фильтров и fsmonitor), без транспорта (ленивая
    # докачка частичного клона), без системного и глобального конфига, от владельца каталога .git
    assert src.count(" git -c ") == 1 and "ls-tree -z --full-tree HEAD" in src and "protocol.allow=never" in src
    assert "GIT_CONFIG_NOSYSTEM=1" in src and 'setpriv --reuid="$uid"' in src and "safe.directory" in src


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


async def test_postponed_site_files_stay_on_server_and_new_ones_show(client, auth_headers, monkeypatch):
    monkeypatch.setattr(webfiles, "STALE", 10 ** 9)
    f, sid, a, _b = await _seed(client, ["www.shop.example.com"])
    await webfiles.track(f, datetime.now(timezone.utc))
    r = await client.post(f"/api/checks/{a}/tamper-accept", json={"key": "edit", "mode": "later"},
                          headers=auth_headers)
    assert r.status_code == 200 and [x["key"] for x in r.json()["tamper"]["signs"]] == ["php"]
    async with f() as s:
        srv = await s.get(Server, sid)
        assert "js/custom.js" in srv.webfiles["roots"]["2080-11"]["edit"]  # на сервере файл остался
        root = srv.webfiles["roots"]["2080-11"]
        srv.webfiles = {**srv.webfiles, "roots": {"2080-11": {**root, "edit": {
            **root["edit"], "js/menu.js": {"since": NOW.isoformat(), "s": 50}}}}}
        await s.commit()
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        edit = (await s.get(Check, a)).tamper_state["signs"]["edit"]
        assert [x["p"] for x in edit["files"]] == ["js/menu.js"]  # отложенный спрятан, новый виден


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


# ---------- ложные признаки: ассеты, старт пода, вложенные корни, поды одного ворклоада ----------

POD = "web-6d4f8b7c9d-x2k4q"


def pod_root(key, where=POD, path="/app", **kw):
    return {"key": key, "where": where, "path": path, "domains": [], "files": 3, "big": False, **kw}


def test_generated_files_are_not_code():
    assert webfiles.generated("admin/web/assets/f0076b67/jquery.js")
    assert webfiles.generated("web/assets/faf2d0d1/yii.activeForm.js")
    assert webfiles.generated("runtime/cache/a1/b2.php") and webfiles.generated("storage/framework/views/x.php")
    assert webfiles.generated("sites/default/files/php/twig/abc/x.php")
    # php в ассетах приложение не публикует - это признак
    assert not webfiles.generated("web/assets/f0076b67/shell.php")
    assert not webfiles.generated("wp-content/themes/shop/js/custom.js")
    assert not webfiles.generated("src/Cache/Repository.php")
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    st = webfiles.process(st, blk(run(T - 600, n_new=3, new=[
        ["admin/web/assets/f0076b67/jquery.js", T - 700, 285314], ["admin/web/assets/f0076b67/jquery.min.js", T - 700, 87533],
        ["web/assets/f0076b67/up.php", T - 650, 400]])), NOW)
    r = st["roots"]["2080-11"]
    assert list(r["php"]) == ["web/assets/f0076b67/up.php"] and "edit" not in r


def test_files_written_in_one_second_are_a_script():
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    gen = [[f"{app}/config/{f}-local.php", T - 6000, 100] for app in ("admin", "api", "common") for f in ("main", "params")]
    st = webfiles.process(st, blk(run(T - 600, n_changed=6, changed=gen)), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and r["deploy"]["n"] == 6


def test_old_signs_pass_new_rules_once():
    # набрано helper 0.1: ассеты и "правки" нового пода, на хосте - настоящая правка
    st = {"seen": T - 600, "roots": {
        "a": {**pod_root("a"), "at": NOW.isoformat(),
              "edit": {"admin/web/assets/f0076b67/jquery.js": {"since": "x", "s": 1},
                       "common/config/main-local.php": {"since": "x", "s": 1}}},
        "h": {"key": "h", "where": "host", "path": "/var/www/shop", "at": NOW.isoformat(),
              "edit": {"js/custom.js": {"since": "x", "s": 1}, "web/assets/1/a.js": {"since": "x", "s": 1}}}},
        "loose": ["a", "h"]}
    st = webfiles.process(st, blk(roots=()), NOW)
    assert "edit" not in st["roots"]["a"] and list(st["roots"]["h"]["edit"]) == ["js/custom.js"]
    assert [t for _l, t, _k in webfiles.problems(st)] == ["код изменен не через git/деплой в /var/www/shop: custom.js"]
    # второй раз чистка не повторяется: новая правка в контейнере остается
    st["roots"]["a"]["edit"] = {"js/app.js": {"since": "x", "s": 1}}
    assert "edit" in webfiles.process(st, blk(roots=()), NOW)["roots"]["a"]


def test_nested_roots_of_one_pod_do_not_duplicate():
    st = webfiles.process(None, blk(roots=(pod_root("a"), pod_root("b", path="/app/admin", domains=["admin.example.com"]))), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": [], "runs": [{"ts": T - 600, "roots": [
        {"key": "a", "n_new": 1, "new": [["admin/js/menu.js", T, 10]]},
        {"key": "b", "n_new": 1, "new": [["js/menu.js", T, 10]]}]}]}, NOW)
    assert "edit" not in st["roots"]["a"] and list(st["roots"]["b"]["edit"]) == ["js/menu.js"]


def test_pods_of_one_workload_are_one_row():
    pods = [f"web-6d4f8b7c9d-{s}" for s in ("x2k4q", "bc7dn", "m9zst")]
    roots = tuple(pod_root(f"c{i}", where=w) for i, w in enumerate(pods))
    st = webfiles.process(None, blk(roots=roots), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": list(roots), "runs": [{"ts": T - 600, "roots": [
        {"key": "c0", "n_new": 1, "new": [["shell.php", T, 900]]},
        {"key": "c2", "n_new": 1, "new": [["shell.php", T, 900]]}]}]}, NOW)
    st["loose"] = ["c0", "c1", "c2"]
    v = webfiles.view(st, {}, NOW)["roots"]
    assert len(v) == 1 and v[0]["where"] == "web" and v[0]["pods"] == 3 and v[0]["key"] == "c0,c1,c2"
    assert [f["p"] for f in v[0]["php"]] == ["shell.php"]
    assert webfiles.problems(st) == [(2, "похоже на взлом: новые php в /app (web): shell.php", "php:web:/app:shell.php")]
    # "это наше" на строке снимает со всех подов
    st = webfiles.accept(st, v[0]["key"], "php")
    assert webfiles.problems(st) == []
    assert webfiles.where("node-a", st["roots"]["c0"]) == "node-a, web"
    # helper 0.2 пишет и неймспейс: одноименные деплойменты разных окружений не склеиваются
    assert webfiles.workload({"where": "stage/web-6d4f8b7c9d-x2k4q"}) == "stage/web"
    assert webfiles.workload({"where": "default/web-6d4f8b7c9d-x2k4q"}) == "web"
    assert webfiles.workload({"where": "host"}) == ""


def test_gone_container_root_drops_in_hours_host_root_stays():
    st = webfiles.process(None, blk(roots=(pod_root("c0"), {**ROOT, "key": "h", "where": "host"})), NOW - timedelta(hours=3))
    st = webfiles.process(st, blk(roots=()), NOW)
    assert list(st["roots"]) == ["h"]


def test_site_sign_lists_a_file_of_many_pods_once():
    st = {"signs": {"php": {"since": NOW.isoformat(), "files": [
        {"p": "shell.php", "sid": 1, "root": "c0", "where": "node-a, web"},
        {"p": "shell.php", "sid": 1, "root": "c2", "where": "node-a, web"}]}}}
    row = next(r for r in tamper.view(st)["signs"] if r["key"] == "php")
    assert row["files"] == ["shell.php"] and row["n"] == 1 and row["where"] == "node-a, web"
    assert tamper.alert_text(st).splitlines()[1] == "Новые php: shell.php"


# ---------- выкладка через git: reflog репозитория, в котором лежит корень ----------

def git_root(events, key="2080-11", **kw):
    return {**ROOT, "key": key, "git": events, **kw}


def test_git_pull_of_two_files_is_a_deploy_an_edit_later_is_a_sign():
    pull = T - 700
    roots = (git_root([[T - 90000, "clone"], [pull - 3, "checkout"], [pull, "pull"]]),)
    st = webfiles.process(None, blk(run(T - 1200, first=True), roots=roots), NOW)
    st = webfiles.process(st, blk(run(T - 600, n_new=1, n_changed=2, changed=[
        ["controllers/ReportController.php", pull - 1, 900], ["controllers/HookController.php", pull - 1, 400]],
        new=[["models/Invoice.php", pull, 300]]), roots=roots), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and "php" not in r
    assert r["deploy"] == {"at": webfiles._iso(pull), "n": 3, "git": "pull"}  # noqa: SLF001
    # правка руками через минуты после pull - признак, как раньше
    st = webfiles.process(st, blk(run(T - 300, n_changed=1, changed=[["controllers/OrderController.php", T - 400, 50]]),
                                  roots=roots), NOW)
    assert list(st["roots"]["2080-11"]["edit"]) == ["controllers/OrderController.php"]
    # php в загрузках - признак и в выкладке через git
    st = webfiles.process(st, blk(run(T - 100, n_new=1, new=[["web/uploads/x.php", pull, 900]]), roots=roots), NOW)
    assert list(st["roots"]["2080-11"]["php"]) == ["web/uploads/x.php"]


def test_without_reflog_two_files_are_a_sign_as_before():
    st = webfiles.process(None, blk(run(T - 1200, first=True)), NOW)
    st = webfiles.process(st, blk(run(T - 600, n_changed=2, changed=[
        ["controllers/ReportController.php", T - 700, 900], ["controllers/HookController.php", T - 700, 400]])), NOW)
    r = st["roots"]["2080-11"]
    assert sorted(r["edit"]) == ["controllers/HookController.php", "controllers/ReportController.php"]
    assert r["edit"]["controllers/HookController.php"]["t"] == T - 700
    # helper обновили: reflog показал pull в ту же секунду - признак уходит сам
    st = webfiles.process(st, blk(roots=(git_root([[T - 699, "pull"]]),)), NOW)
    assert "edit" not in st["roots"]["2080-11"] and st["roots"]["2080-11"]["deploy"]["git"] == "pull"


def test_old_sign_without_file_time_goes_with_reflog_of_its_run():
    since = datetime.fromtimestamp(T - 600, timezone.utc).isoformat()
    st = {"r": webfiles.RULES, "seen": T - 600, "roots": {
        "a": {**ROOT, "key": "a", "where": "host", "at": NOW.isoformat(),
              "edit": {"controllers/ReportController.php": {"since": since, "s": 1}}},
        "b": {**ROOT, "key": "b", "where": "host", "path": "/srv/b", "at": NOW.isoformat(),
              "edit": {"js/app.js": {"since": since, "s": 1}}}}}
    roots = (git_root([[T - 640, "pull"]], key="a", where="host"),
             git_root([[T - 5000, "pull"]], key="b", where="host", path="/srv/b"))
    st = webfiles.process(st, blk(roots=roots), NOW)
    assert "edit" not in st["roots"]["a"]
    assert list(st["roots"]["b"]["edit"]) == ["js/app.js"]  # pull задолго до признака - правка остается


def test_one_dir_in_two_containers_is_one_row():
    roots = (pod_root("c1", where="shop-php-fpm", path="/srv/shop", src="2049-77"),
             pod_root("c2", where="shop-worker", path="/srv/shop", src="2049-77"),
             pod_root("c3", where="shop-nginx", path="/srv/shop/backend", src="2049-90"))
    st = webfiles.process(None, blk(roots=roots), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": list(roots), "runs": [{"ts": T - 600, "roots": [
        {"key": "c1", "n_changed": 1, "changed": [["backend/a.js", T - 900, 10]]},
        {"key": "c2", "n_changed": 1, "changed": [["backend/a.js", T - 900, 10]]}]}]}, NOW)
    st["loose"] = ["c1", "c2", "c3"]
    v = webfiles.view(st, {}, NOW)["roots"]
    assert [(x["where"], x["path"], x["key"], x["pods"]) for x in v] == [
        ("shop-php-fpm, shop-worker", "/srv/shop", "c1,c2", 1), ("shop-nginx", "/srv/shop/backend", "c3", 1)]
    assert [f["p"] for f in v[0]["edit"]] == ["backend/a.js"]
    assert webfiles.problems(st) == [(1, "код изменен не через git/деплой в /srv/shop (shop-php-fpm, shop-worker): a.js",
                                      "edit:shop-php-fpm, shop-worker:/srv/shop:backend/a.js")]



# ---------- WordPress обновляет себя сам ----------

WP_ROOT = {**ROOT, "wp": "6.6.2"}
LANG = "wp-content/languages/plugins/wordpress-seo-ru_RU.l10n.php"


def wp_start():
    return webfiles.process(None, blk(run(T - 1200, first=True), roots=(WP_ROOT,)), NOW)


def test_language_pack_is_wordpress_not_an_edit():
    # пакет перевода: .l10n.php и .mo в одну секунду - не признак, серая строка
    st = webfiles.process(wp_start(), blk(run(T - 600, n_changed=1, changed=[[LANG, T - 700, 52000, T - 700]]),
                                          roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and "deploy" not in r
    assert r["wpev"] == [{"k": "lang", "part": "plugin", "slug": "wordpress-seo", "loc": "ru_RU",
                          "at": datetime.fromtimestamp(T - 700, timezone.utc).isoformat()}]
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st) == []
    v = webfiles.view(st, {}, NOW)["roots"][0]
    assert v["wp"] == "6.6.2" and [e["slug"] for e in v["wp_events"]] == ["wordpress-seo"]
    # перевод ядра и темы
    st = webfiles.process(st, blk(run(T - 300, n_changed=2, changed=[
        ["wp-content/languages/admin-ru_RU.l10n.php", T - 400, 9000, T - 401],
        ["wp-content/languages/themes/twentytwentyfour-de_DE_formal.l10n.php", T - 400, 900, T - 400]]),
        roots=(WP_ROOT,)), NOW)
    evs = st["roots"]["2080-11"]["wpev"]
    assert {(e["part"], e["slug"], e["loc"]) for e in evs} == {
        ("plugin", "wordpress-seo", "ru_RU"), ("core", "", "ru_RU"), ("theme", "twentytwentyfour", "de_DE_formal")}
    assert "edit" not in st["roots"]["2080-11"]


def test_language_file_without_its_mo_stays_a_sign():
    # .mo не обновлялся (правка руками) или helper старый (времени .mo нет) - правка, как раньше
    st = webfiles.process(wp_start(), blk(run(T - 600, n_changed=2, changed=[
        [LANG, T - 700, 52000, T - 90000], ["wp-content/languages/ru_RU.l10n.php", T - 700, 9000]]),
        roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert sorted(r["edit"]) == ["wp-content/languages/plugins/wordpress-seo-ru_RU.l10n.php",
                                 "wp-content/languages/ru_RU.l10n.php"]
    assert "wpev" not in r
    # php в загрузках с "временем .mo" рядом - все равно признак
    st = webfiles.process(st, blk(run(T - 300, n_new=1, new=[
        ["wp-content/uploads/languages/x-ru_RU.l10n.php", T - 400, 900, T - 400]]), roots=(WP_ROOT,)), NOW)
    assert list(st["roots"]["2080-11"]["php"]) == ["wp-content/uploads/languages/x-ru_RU.l10n.php"]


def test_plugin_update_with_version_is_info_without_it_a_named_sign():
    st = wp_start()
    up = ["plugin", "hello-dolly", "1.7.2", "1.7.3", "wp-content/plugins/hello-dolly/"]
    st = webfiles.process(st, blk(run(T - 600, n_changed=2, wp=[up], changed=[
        ["wp-content/plugins/hello-dolly/hello.php", T - 700, 2500],
        ["wp-content/plugins/hello-dolly/readme.js", T - 699, 300]]), roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and "deploy" not in r
    assert r["wpev"][0] == {"k": "plugin", "slug": "hello-dolly", "from": "1.7.2", "to": "1.7.3",
                            "at": datetime.fromtimestamp(T - 700, timezone.utc).isoformat()}
    # большое обновление: в списке 15 файлов из 400, все его - ни выкладки, ни признака
    many = [[f"wp-content/plugins/wordpress-seo/src/f{i:02}.php", T - 500, 100] for i in range(15)]
    st = webfiles.process(st, blk(run(T - 450, n_changed=400, changed=many, wp=[
        ["plugin", "wordpress-seo", "23.5", "23.6", "wp-content/plugins/wordpress-seo/"]]), roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and "deploy" not in r and r["wpev"][0]["to"] == "23.6"
    # файлы плагина изменились, версия та же - признак, с названием плагина
    st = webfiles.process(st, blk(run(T - 300, n_changed=1, changed=[
        ["wp-content/plugins/wordpress-seo/src/main.php", T - 350, 4100]]), roots=(WP_ROOT,)), NOW)
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st) == [(1, "файлы плагина wordpress-seo изменились без смены версии в /var/www/html "
                                         "(wordpress-7d9f): main.php",
                                      "edit:wordpress-7d9f:/var/www/html:wp-content/plugins/wordpress-seo/src/main.php")]


def test_core_file_changed_without_version_is_a_sign():
    st = wp_start()
    # ядро обновилось: версия сменилась, файлы ядра вместе - не признак; wp-config.php - правка
    st = webfiles.process(st, blk(run(T - 600, n_changed=3, wp=[["core", "", "6.6.1", "6.6.2", ""]], changed=[
        ["wp-includes/version.php", T - 700, 900], ["wp-login.php", T - 700, 5000],
        ["wp-config.php", T - 700, 3000]]), roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert list(r["edit"]) == ["wp-config.php"] and r["wpev"][0]["k"] == "core"
    st = webfiles.accept(st)
    # файл ядра поменяли без смены версии - признак с названием
    st = webfiles.process(st, blk(run(T - 300, n_changed=1, changed=[["wp-includes/post.php", T - 350, 900]]),
                                  roots=(WP_ROOT,)), NOW)
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st)[0][1] == ("файлы ядра WordPress изменились без смены версии в /var/www/html "
                                           "(wordpress-7d9f): post.php")
    # правка задолго до обновления в том же каталоге - не его (файлы обновления пишутся подряд)
    st = webfiles.accept(st)
    st = webfiles.process(st, blk(run(T - 100, n_changed=2, wp=[["core", "", "6.6.2", "6.6.3", ""]], changed=[
        ["wp-includes/post.php", T - 5000, 900], ["wp-includes/version.php", T - 150, 900]]),
        roots=(WP_ROOT,)), NOW)
    assert list(st["roots"]["2080-11"]["edit"]) == ["wp-includes/post.php", "wp-includes/version.php"]


def test_edit_head_names_the_owner():
    assert webfiles.edit_head(["wp-content/themes/shop/js/a.js"], False) == "файлы темы shop изменились"
    assert webfiles.edit_head(["wp-content/plugins/hello.php"], True) == "файлы плагина hello изменились без смены версии"
    # index.php в корне - ядро, только если корень точно WordPress
    assert webfiles.edit_head(["index.php"], False) == "код изменен не через git/деплой"
    assert webfiles.edit_head(["index.php"], True) == "файлы ядра WordPress изменились без смены версии"
    assert webfiles.edit_head(["wp-content/plugins/a/x.php", "js/app.js"], True) == "код изменен не через git/деплой"


# ---------- рабочая копия git: тот же файл, что в HEAD, или правка ----------

GIT_ROOT = {**ROOT, "where": "host", "path": "/srv/api", "branch": "feature-x"}
SRC = "src/Service/Report/Column.php"


def git_start():
    return webfiles.process(None, blk(run(T - 1200, first=True), roots=(GIT_ROOT,)), NOW)


def test_same_content_as_head_is_not_an_edit():
    # файл записан заново тем же содержимым (IDE, инструмент в контейнере) - не признак, тихая строка
    st = webfiles.process(git_start(), blk(run(T - 600, n_changed=1, changed=[[SRC, T - 700, 900]], git_same=[SRC]),
                                           roots=(GIT_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edit" not in r and "deploy" not in r and r["same"]["files"] == [SRC]
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st) == []
    v = webfiles.view(st, {}, NOW)["roots"][0]
    assert v["branch"] == "feature-x" and v["same"]["n"] == 1
    # правку откатили к HEAD - признак уходит сам
    st = webfiles.process(st, blk(run(T - 300, n_changed=1, changed=[[SRC, T - 350, 950]], git_dirty=[SRC]),
                                  roots=(GIT_ROOT,)), NOW)
    assert list(st["roots"]["2080-11"]["edit"]) == [SRC]
    st = webfiles.process(st, blk(run(T - 100, n_changed=1, changed=[[SRC, T - 150, 900]], git_same=[SRC]),
                                  roots=(GIT_ROOT,)), NOW)
    assert "edit" not in st["roots"]["2080-11"]


def test_uncommitted_edit_is_named_so():
    st = webfiles.process(git_start(), blk(run(T - 600, n_changed=1, changed=[[SRC, T - 700, 950]], git_dirty=[SRC]),
                                           roots=(GIT_ROOT,)), NOW)
    assert st["roots"]["2080-11"]["edit"][SRC]["g"] == "dirty"
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st)[0][:2] == (1, "не закоммиченная правка в рабочей копии git (ветка feature-x) "
                                               "в /srv/api: Column.php")
    # рядом файл, которого нет в git, - обычная правка, без "не закоммиченной"
    st = webfiles.process(st, blk(run(T - 300, n_new=1, new=[["public/js/x.js", T - 350, 10]]), roots=(GIT_ROOT,)), NOW)
    assert webfiles.problems(st)[0][1].startswith("код изменен не через git/деплой в /srv/api")
    # в карточке сайта: признак знает, что правка в рабочей копии
    by_check, _loose = webfiles.mirror([(1, "dev", {**st, "roots": {"2080-11": {
        **st["roots"]["2080-11"], "edit": {SRC: st["roots"]["2080-11"]["edit"][SRC]}}}})], [(5, "", {1})])
    row = next(x for x in tamper.view({"signs": by_check[5]})["signs"] if x["key"] == "edit")
    assert row["dirty"] is True and row["branch"] == "feature-x"


def test_git_does_not_hide_break_in_files():
    # php в загрузках с тем же содержимым, что в HEAD, и новый php вне git - признаки, как раньше
    st = webfiles.process(git_start(), blk(run(T - 600, n_new=2, new=[
        ["public/uploads/shell.php", T - 700, 900], ["src/Controller/x.php", T - 700, 300]],
        git_same=["public/uploads/shell.php"]), roots=(GIT_ROOT,)), NOW)
    assert sorted(st["roots"]["2080-11"]["php"]) == ["public/uploads/shell.php", "src/Controller/x.php"]
    assert "same" not in st["roots"]["2080-11"]
