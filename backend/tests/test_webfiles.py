"""Признаки взлома в файлах сайтов (app/webfiles.py, helper webfiles-setup): красное только по
уликам (веб-шелл, php в загрузках, запуск php в .htaccess, файл WordPress не как в выпуске),
остальное - серые строки; привязка корня к монитору, проблема и алерт сервера, кнопки."""
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import collector, setup_scripts, tamper, webfiles
from app.models import Check, Server

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
ROOT = {"key": "2080-11", "where": "wordpress-7d9f", "path": "/var/www/html", "domains": [], "files": 2400,
        "big": False}
SRC_FILE = Path(__file__).resolve().parents[2] / "agent" / "webfiles-setup.sh"


def blk(*runs, ts=T, roots=(ROOT,)):
    return {"v": 1, "ts": ts, "roots": list(roots), "runs": list(runs)}


def run(ts, **d):
    return {"ts": ts, "roots": [{"key": ROOT["key"], **d}]}


def start(roots=(ROOT,)):
    return webfiles.process(None, blk(run(T - 1200, first=True), roots=roots), NOW)


def test_only_evidence_is_a_sign():
    st = start()
    assert st["roots"]["2080-11"]["where"] == "wordpress-7d9f" and "php" not in st["roots"]["2080-11"]
    st = webfiles.process(st, blk(run(T - 600, n_new=2, n_changed=1,
                                      new=[["wp-content/uploads/2026/10/lock360.php", T - 650, 900], ["about.php", T - 650, 90]],
                                      changed=[["wp-content/themes/shop/js/prod/custom.js", T - 640, 4100]])), NOW)
    r = st["roots"]["2080-11"]
    # php в загрузках - улика; новый php в корне и правка js - серая строка "что менялось"
    assert list(r["php"]) == ["wp-content/uploads/2026/10/lock360.php"] and r["php"][
        "wp-content/uploads/2026/10/lock360.php"]["y"] == "upload"
    assert r["edits"]["n"] == 2 and r["edits"]["files"] == ["about.php", "wp-content/themes/shop/js/prod/custom.js"]
    assert "edit" not in r
    # тот же блок второй раз (агент шлет файл каждую минуту) - ничего не меняет
    assert webfiles.process(st, blk(run(T - 600, n_new=1, new=[["x.php", T, 10]])), NOW)["roots"] == st["roots"]
    # файл удалили - признак уходит сам
    st = webfiles.process(st, blk(run(T - 300, n_gone=1, gone=[["wp-content/uploads/2026/10/lock360.php", 0, 0]])), NOW)
    assert "php" not in st["roots"]["2080-11"]


def test_uploads_are_data_dirs_not_code():
    for p in ("wp-content/uploads/2026/10/x.php", "public/uploads/x.php", "upload/iblock/x.php", "web/tmp/a.php",
              "sites/default/files/x.php", "public_html/wp-content/uploads/a/b.php", "userfiles/x.php"):
        assert webfiles.in_uploads(p), p
    # каталоги files, images, media, upload в коде - обычные классы (в живом коде 354 таких файла)
    for p in ("wp-includes/php-ai-client/src/Files/DTO/File.php", "src/Media/Image.php", "app/Images/Resize.php",
              "wp-content/plugins/updraftplus/vendor/rackspace/lib/ObjectStore/Upload/Transfer.php",
              "wp-content/plugins/wordpress-seo/src/images/Application/x.php", "vendor/x/files/a.php",
              "wp-content/uploads.php", "uploads.php"):
        assert not webfiles.in_uploads(p), p


def test_deploy_is_a_gray_row_php_in_uploads_is_a_sign():
    st = start()
    many = [[f"wp-content/plugins/seo/inc/f{i}.php", T - 640, 100] for i in range(9)]
    st = webfiles.process(st, blk(run(T - 600, n_new=12, n_changed=30, new=many + [
        ["wp-content/uploads/cache.php", T - 640, 300], ["wp-content/uploads/index.php", T - 640, 28]],
        changed=[["wp-includes/version.php", T - 640, 900]],
        f={"wp-includes/version.php": ["", "deploy", 0, 0], "wp-content/plugins/seo/inc/f1.php": ["", "deploy", 0, 0]})),
        NOW)
    r = st["roots"]["2080-11"]
    assert r["deploy"]["n"] == 41 and r["deploy"]["o"] == ["deploy"] and "git" not in r["deploy"]
    assert list(r["php"]) == ["wp-content/uploads/cache.php"]  # index.php-заглушка не в счет
    assert "edits" not in r


def test_owners_by_domain_or_single_site():
    checks = [(1, "www.shop.example.com", {7}), (2, "blog.example.org", {7}), (3, "api.example.net", {9})]
    assert webfiles.owners({"domains": ["shop.example.com"]}, 7, checks) == [1]
    assert webfiles.owners({"domains": ["*.example.net"]}, 7, checks) == [3]
    assert webfiles.owners({"domains": []}, 9, checks) == [3]
    assert webfiles.owners({"domains": []}, 7, checks) == []  # на сервере два сайта - не угадываем


def _server(st):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report={}, alert_state={},
                  alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
                  disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
                  disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[],
                  webfiles=st)


def test_server_problem_and_alert():
    st = webfiles.process(start(), blk(run(T - 600, n_new=2, new=[
        ["wp-content/uploads/lock360.php", T, 900], ["wp-content/uploads/about.php", T, 900]])), NOW)
    st["loose"] = ["2080-11"]
    probs = webfiles.problems(st)
    assert probs == [(2, "похоже на взлом: php в загрузках в /var/www/html (wordpress-7d9f): about.php, lock360.php",
                      "php:wordpress-7d9f:/var/www/html:wp-content/uploads/about.php,wp-content/uploads/lock360.php")]
    assert webfiles.tg_short(probs) == ("Похоже на взлом: php в загрузках", ["about.php, lock360.php",
                                                                            "/var/www/html (wordpress-7d9f)"])
    s = _server(st)
    lvl, ctx = collector._server_conditions(s, NOW)["webfiles"]
    assert lvl == 3 and ctx["sig"] == [probs[0][2]]
    rows = [(p["level"], p.get("section"), p["sec"]) for p in collector.server_problems(s, NOW) if p["kind"] == "webfiles"]
    assert rows == [(2, "services", "files")]
    # правки без улик - ни строки в "Что сломано", ни Telegram
    s.webfiles = {"roots": {"k": {"path": "/srv/app", "where": "host", "edits": {"at": NOW.isoformat(), "n": 1}}},
                  "loose": ["k"]}
    assert collector._server_conditions(s, NOW)["webfiles"][0] == 0
    assert [p for p in collector.server_problems(s, NOW) if p["kind"] == "webfiles"] == []
    assert collector._recovery_detail("webfiles", {}, {}) == "признаков взлома в файлах сайтов больше нет"


def test_site_signs_count_with_page_signs():
    st = {"signs": {"php": {"since": NOW.isoformat(), "files": [
        {"p": "wp-content/uploads/lock360.php", "y": "upload", "sid": 1, "root": "k", "where": "web-a, wordpress"},
        {"p": "wp-content/uploads/8bd31397/about.php", "y": "upload"}]}}}
    assert tamper.level(st) == "hack" and tamper.summary(st) == "похоже на взлом: php в загрузках (lock360.php, about.php)"
    row = next(r for r in tamper.view(st)["signs"] if r["key"] == "php")
    assert row["files"][0] == "wp-content/uploads/lock360.php" and row["where"] == "web-a, wordpress"
    assert row["items"][0]["y"] == "upload"
    assert tamper.alert_text(st).splitlines()[1] == "php в загрузках: lock360.php, about.php"
    st["signs"]["php"]["files"] = [{"p": "wp-includes/user.php", "y": "wporg", "ref": "core", "ver": "6.9.10"}]
    assert tamper.alert_text(st).splitlines()[1] == "Файлы ядра WordPress не как в выпуске 6.9.10: user.php"


def test_helper_needed_and_written_right():
    assert setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "nginx"}]})
    assert setup_scripts.setup_needed("webfiles-setup", {"extras": {"fpm": {"v": 1, "pools": [{"pool": "www"}]}}})
    # fpm-setup стоит всюду: без PHP его пустой блок не повод, на ноде условие скрипта ложно
    assert not setup_scripts.setup_needed("webfiles-setup", {"extras": {"fpm": {"v": 1, "pools": [], "skipped": []}}})
    assert not setup_scripts.setup_needed("webfiles-setup", {"web_services": [{"kind": "Traefik"}]})
    src = SRC_FILE.read_text(encoding="utf-8")
    assert "\r" not in src and "KERVAX_SETUP_VERSION=0.8 " in src and "report.d/webfiles.json" in src
    assert 'mv -f "$HELPER.new" "$HELPER"' in src and "versions/webfiles-setup.ver" in src
    # контейнер: только верхний слой overlay, ключ по контейнеру, старт - не правка, ассеты мимо
    assert "upperdir=" in src and 'key="c${cid:0:12}' in src and "START_WIN=600" in src
    assert "! -path '*/web/assets/*'" in src
    # выкладка через git: reflog читается файлом, сам git не запускается (конфиг репозитория мог бы
    # запустить фильтр от root), корень в bind-mount ищется на хосте через mountinfo
    assert "/logs/HEAD" in src and "/proc/1/mountinfo" in src and '\\"git\\":[$GEV]' in src
    assert "git diff" not in src and "git -C" not in src and "git status" not in src and "hash-object" not in src
    # сверка с HEAD: от git только ls-tree и check-ignore (без хуков, фильтров и fsmonitor), без
    # транспорта (ленивая докачка частичного клона), без системного и глобального конфига, от
    # владельца каталога .git
    assert src.count(" git -c ") == 2 and src.count("protocol.allow=never") == 2 and "ls-tree -z --full-tree HEAD" in src
    assert "GIT_CONFIG_NOSYSTEM=1" in src and 'setpriv --reuid="$uid"' in src and "safe.directory" in src


def test_helper_08_owner_exec_and_sums():
    src = SRC_FILE.read_text(encoding="utf-8")
    # смонтированный в контейнер каталог: имя владельца с хоста (выкладка из CI), uid, которых хост
    # не знает, - из контейнера; верхний слой контейнера - по его passwd
    assert 'owners_of "$lroot" "$pw" "$WEBALL" "$work/listed" "$pw2"' in src
    assert 'if [ "$layer" = true ]; then pw=/proc/$pid/root/etc/passwd; else pw2=/proc/$pid/root/etc/passwd; fi' in src
    # запуск php в .htaccess и .user.ini - шестым полем файла
    assert 'exec_check "$p" "$work/listed"' in src and ',\\"$d\\",$e]' in src
    # md5 файлов ядра и плагинов WordPress - только для сверки с wordpress.org, локаль ядра в корне
    assert 'wp_sums "$p" "$work/wp" "$work/listed"' in src and '\\"sums\\":{$fsums}' in src
    assert "md5sum" in src and "$wp_local_package" in src and '\\"wploc\\":\\"$wploc\\"' in src
    # метод ($db->exec($_POST)) и функция с другим именем (curl_exec) - не веб-шелл
    assert "(^|[^A-Za-z0-9_>:$\\])(eval|assert|system" in src


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
    async with f() as s:
        # признак правки от прошлых версий панели в карточке сайта снимается
        ca = await s.get(Check, a)
        ca.tamper_state = {"signs": {"edit": {"since": NOW.isoformat(), "files": [{"p": "js/custom.js"}]}}}
        await s.commit()
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        ca, cb, srv = await s.get(Check, a), await s.get(Check, b), await s.get(Server, sid)
        assert set(ca.tamper_state["signs"]) == {"php"} and not cb.tamper_state
        assert not srv.webfiles.get("loose")
    r = await client.post(f"/api/checks/{a}/tamper-accept", json={"key": "php"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["tamper"]["signs"] == []
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        assert not (await s.get(Check, a)).tamper_state["signs"]  # принятый не вернулся
        assert "php" not in (await s.get(Server, sid)).webfiles["roots"]["2080-11"]


async def test_postponed_site_files_stay_on_server_and_new_ones_show(client, auth_headers, monkeypatch):
    monkeypatch.setattr(webfiles, "STALE", 10 ** 9)
    f, sid, a, _b = await _seed(client, ["www.shop.example.com"])
    await webfiles.track(f, datetime.now(timezone.utc))
    r = await client.post(f"/api/checks/{a}/tamper-accept", json={"key": "php", "mode": "later"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["tamper"]["signs"] == []
    async with f() as s:
        srv = await s.get(Server, sid)
        root = srv.webfiles["roots"]["2080-11"]
        assert "wp-content/uploads/lock360.php" in root["php"]  # на сервере файл остался
        srv.webfiles = {**srv.webfiles, "roots": {"2080-11": {**root, "php": {
            **root["php"], "wp-content/uploads/x.php": {"since": NOW.isoformat(), "s": 50, "y": "upload"}}}}}
        await s.commit()
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        php = (await s.get(Check, a)).tamper_state["signs"]["php"]
        assert [x["p"] for x in php["files"]] == ["wp-content/uploads/x.php"]  # отложенный спрятан, новый виден


async def test_track_loose_root_is_server_problem(client, auth_headers, monkeypatch):
    monkeypatch.setattr(webfiles, "STALE", 10 ** 9)
    # данные засеяны на NOW, а карточка сверяет с настоящим временем: без этого тест протухает через неделю
    monkeypatch.setattr(webfiles, "WP_SHOW", timedelta(days=10 ** 5))
    f, sid, a, _b = await _seed(client, [])
    await webfiles.track(f, datetime.now(timezone.utc))
    async with f() as s:
        assert (await s.get(Server, sid)).webfiles["loose"] == ["2080-11"]
        assert not (await s.get(Check, a)).tamper_state
    r = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    wf = r.json()["webfiles"]["roots"][0]
    assert wf["php"][0]["p"] == "wp-content/uploads/lock360.php" and wf["php"][0]["y"] == "upload"
    assert wf["site"] is False and wf["edits"]["files"] == ["js/custom.js"] and "edit" not in wf and "mode" not in wf
    r = await client.post(f"/api/servers/{sid}/webfiles-accept", json={"root": "2080-11"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["webfiles"]["roots"][0]["php"] == []
    async with f() as s:
        assert webfiles.problems((await s.get(Server, sid)).webfiles) == []
    # "здесь правят руками" больше нет: правка не признак нигде
    r = await client.post(f"/api/servers/{sid}/webfiles-mode", json={"root": "2080-11", "mode": "dev"},
                          headers=auth_headers)
    assert r.status_code in (404, 405)


def test_old_roots_drop_after_a_week():
    st = webfiles.process(None, blk(run(T - 600, first=True)), NOW - timedelta(days=8))
    st = webfiles.process(st, blk(roots=()), NOW)
    assert st["roots"] == {}


def test_old_signs_pass_new_rules_once():
    # набрано до правил 4: правки, новый php вне загрузок (стенд: миграция из выкладки), *-local.php,
    # выбор "правят руками"; остаются только улики, с причиной
    st = {"r": 3, "seen": T - 600, "modes": {"web:/app": "dev"}, "roots": {
        "a": {**ROOT, "key": "a", "where": "mesh-stage-php-1", "path": "/app", "at": NOW.isoformat(),
              "php": {"console/migrations/m261024_120000_rename.php": {"since": "x", "s": 900, "k": "new", "o": "systemd-timesync"}},
              "edit": {"common/models/User.php": {"since": "x", "s": 1}}},
        "b": {**ROOT, "key": "b", "where": "host", "path": "/var/www/shop", "at": NOW.isoformat(),
              "php": {"wp-content/uploads/x.php": {"since": "x", "s": 40}, "src/a.php": {"since": "x", "s": 9, "x": 1},
                      "wp-content/uploads/index.php": {"since": "x", "s": 28}},
              "edit": {"js/custom.js": {"since": "x", "s": 1}}}}, "loose": ["a", "b"]}
    st = webfiles.process(st, blk(roots=()), NOW)
    assert "php" not in st["roots"]["a"] and "edit" not in st["roots"]["a"] and "modes" not in st
    assert {p: v["y"] for p, v in st["roots"]["b"]["php"].items()} == {"wp-content/uploads/x.php": "upload",
                                                                      "src/a.php": "shell"}
    assert "edit" not in st["roots"]["b"] and st["r"] == webfiles.RULES
    assert [t for _l, t, _k in webfiles.problems(st)] == ["похоже на взлом: признаки взлома в файлах в /var/www/shop: a.php, x.php"]


# ---------- ложные признаки: ассеты, старт пода, вложенные корни, поды одного ворклоада ----------

POD = "web-6d4f8b7c9d-x2k4q"


def pod_root(key, where=POD, path="/app", **kw):
    return {"key": key, "where": where, "path": path, "domains": [], "files": 3, "big": False, **kw}


def test_generated_files_are_not_code():
    assert webfiles.generated("admin/web/assets/f0076b67/jquery.js")
    assert webfiles.generated("web/assets/faf2d0d1/yii.activeForm.js")
    assert webfiles.generated("runtime/cache/a1/b2.php") and webfiles.generated("storage/framework/views/x.php")
    assert webfiles.generated("sites/default/files/php/twig/abc/x.php")
    assert webfiles.generated("wp-content/wflogs/attack-data.php")
    assert not webfiles.generated("web/assets/f0076b67/shell.php")
    assert not webfiles.generated("wp-content/themes/shop/js/custom.js")
    assert not webfiles.generated("src/Cache/Repository.php")
    st = webfiles.process(start(), blk(run(T - 600, n_new=2, new=[
        ["admin/web/assets/f0076b67/jquery.js", T - 700, 285314], ["admin/web/assets/f0076b67/jquery.min.js", T - 700, 87533]])),
        NOW)
    assert "edits" not in st["roots"]["2080-11"] and "php" not in st["roots"]["2080-11"]


def test_files_written_in_one_second_are_a_script():
    gen = [[f"{app}/config/{f}-local.php", T - 6000, 100] for app in ("admin", "api", "common") for f in ("main", "params")]
    st = webfiles.process(start(), blk(run(T - 600, n_changed=6, changed=gen)), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and r["deploy"]["n"] == 6 and "php" not in r


def test_stage_deploy_without_git_is_one_gray_row():
    # выкладка на стенд из CI (rsync, reflog только clone): миграция, модель, ассеты с хешем за две
    # минуты; владелец - пользователь хоста, который пишет выкладку
    roots = (pod_root("c1", where="mesh-stage-php-1", git=[[T - 90 * 86400, "clone"]], branch="master"),)
    files = [["console/migrations/m261024_120000_rename_dev_user_accounts.php", T - 732, 1800],
             ["common/models/User.php", T - 732, 9000],
             ["backend/web/kb/assets/javascripts/bundle.c21346df.min.js", T - 635, 90000],
             ["backend/web/kb/assets/javascripts/workers/search.a7633da7.min.js", T - 635, 9000]]
    f = {p: ["", "gitlab-runner", 0, 0, "", 0] for p, _t, _s in files}
    st = webfiles.process(webfiles.process(None, blk(run(T - 1200, first=True), roots=roots), NOW),
                          {"v": 1, "ts": T, "roots": list(roots), "runs": [{"ts": T - 600, "roots": [
                              {"key": "c1", "n_new": 3, "n_changed": 11, "new": [files[0]] + files[2:],
                               "changed": [files[1]], "f": f}]}]}, NOW)
    r = st["roots"]["c1"]
    assert "php" not in r and "edits" not in r
    assert r["deploy"] == {"at": webfiles._iso(T - 635), "from": webfiles._iso(T - 732), "n": 14,  # noqa: SLF001
                           "files": sorted(p for p, _t, _s in files), "o": ["gitlab-runner"]}
    st["loose"] = ["c1"]
    assert webfiles.problems(st) == []


def test_nested_roots_of_one_pod_do_not_duplicate():
    st = webfiles.process(None, blk(roots=(pod_root("a"), pod_root("b", path="/app/admin", domains=["admin.example.com"]))), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": [], "runs": [{"ts": T - 600, "roots": [
        {"key": "a", "n_new": 1, "new": [["admin/uploads/menu.php", T, 10]]},
        {"key": "b", "n_new": 1, "new": [["uploads/menu.php", T, 10]]}]}]}, NOW)
    assert "php" not in st["roots"]["a"] and list(st["roots"]["b"]["php"]) == ["uploads/menu.php"]


def test_pods_of_one_workload_are_one_row():
    pods = [f"web-6d4f8b7c9d-{s}" for s in ("x2k4q", "bc7dn", "m9zst")]
    roots = tuple(pod_root(f"c{i}", where=w) for i, w in enumerate(pods))
    st = webfiles.process(None, blk(roots=roots), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": list(roots), "runs": [{"ts": T - 600, "roots": [
        {"key": "c0", "n_new": 1, "new": [["shell.php", T, 900]], "f": {"shell.php": ["", "www-data", 1, 1]}},
        {"key": "c2", "n_new": 1, "new": [["shell.php", T, 900]], "f": {"shell.php": ["", "www-data", 1, 1]}}]}]}, NOW)
    st["loose"] = ["c0", "c1", "c2"]
    v = webfiles.view(st, {}, NOW)["roots"]
    assert len(v) == 1 and v[0]["where"] == "web" and v[0]["pods"] == 3 and v[0]["key"] == "c0,c1,c2"
    assert [f["p"] for f in v[0]["php"]] == ["shell.php"]
    assert webfiles.problems(st) == [(2, "похоже на взлом: php похож на веб-шелл в /app (web): shell.php, владелец "
                                         "www-data (веб-сервер)", "php:web:/app:shell.php")]
    # "это наше" на строке снимает со всех подов
    st = webfiles.accept(st, v[0]["key"], "php")
    assert webfiles.problems(st) == []
    assert webfiles.where("node-a", st["roots"]["c0"]) == "node-a, web"
    # helper пишет и неймспейс: одноименные деплойменты разных окружений не склеиваются
    assert webfiles.workload({"where": "stage/web-6d4f8b7c9d-x2k4q"}) == "stage/web"
    assert webfiles.workload({"where": "default/web-6d4f8b7c9d-x2k4q"}) == "web"
    assert webfiles.workload({"where": "host"}) == ""


def test_gone_container_root_drops_in_hours_host_root_stays():
    st = webfiles.process(None, blk(roots=(pod_root("c0"), {**ROOT, "key": "h", "where": "host"})), NOW - timedelta(hours=3))
    st = webfiles.process(st, blk(roots=()), NOW)
    assert list(st["roots"]) == ["h"]


def test_site_sign_lists_a_file_of_many_pods_once():
    st = {"signs": {"php": {"since": NOW.isoformat(), "files": [
        {"p": "shell.php", "sid": 1, "root": "c0", "where": "node-a, web", "x": 1},
        {"p": "shell.php", "sid": 1, "root": "c2", "where": "node-a, web", "x": 1}]}}}
    row = next(r for r in tamper.view(st)["signs"] if r["key"] == "php")
    assert row["files"] == ["shell.php"] and row["n"] == 1 and row["where"] == "node-a, web"
    assert tamper.alert_text(st).splitlines()[1] == "php похож на веб-шелл: shell.php"


# ---------- выкладка через git: reflog репозитория, в котором лежит корень ----------

def git_root(events, key="2080-11", **kw):
    return {**ROOT, "key": key, "git": events, **kw}


def test_git_pull_is_a_deploy_an_edit_later_a_gray_row():
    pull = T - 700
    roots = (git_root([[T - 90000, "clone"], [pull - 3, "checkout"], [pull, "pull"]]),)
    st = start(roots)
    st = webfiles.process(st, blk(run(T - 600, n_new=1, n_changed=2, changed=[
        ["controllers/ReportController.php", pull - 1, 900], ["controllers/HookController.php", pull - 1, 400]],
        new=[["models/Invoice.php", pull, 300]], f={"models/Invoice.php": ["", "deploy", 0, 0]}), roots=roots), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "php" not in r
    assert r["deploy"] == {"at": webfiles._iso(pull), "n": 3, "git": "pull", "o": ["deploy"]}  # noqa: SLF001
    # composer после pull, правка руками через минуты - серая строка, не признак
    st = webfiles.process(st, blk(run(T - 300, n_changed=1, changed=[["vendor/composer/installed.php", pull + 4, 50]]),
                                  roots=roots), NOW)
    assert st["roots"]["2080-11"]["edits"]["files"] == ["vendor/composer/installed.php"] and "php" not in st["roots"]["2080-11"]
    # php в загрузках - признак и в выкладке через git
    st = webfiles.process(st, blk(run(T - 100, n_new=1, new=[["web/uploads/x.php", pull, 900]]), roots=roots), NOW)
    assert list(st["roots"]["2080-11"]["php"]) == ["web/uploads/x.php"]


def test_one_dir_in_two_containers_is_one_row():
    roots = (pod_root("c1", where="shop-php-fpm", path="/srv/shop", src="2049-77"),
             pod_root("c2", where="shop-worker", path="/srv/shop", src="2049-77"),
             pod_root("c3", where="shop-nginx", path="/srv/shop/backend", src="2049-90"))
    st = webfiles.process(None, blk(roots=roots), NOW)
    st = webfiles.process(st, {"v": 1, "ts": T, "roots": list(roots), "runs": [{"ts": T - 600, "roots": [
        {"key": "c1", "n_new": 1, "new": [["backend/web/uploads/a.php", T - 900, 10]]},
        {"key": "c2", "n_new": 1, "new": [["backend/web/uploads/a.php", T - 900, 10]]}]}]}, NOW)
    st["loose"] = ["c1", "c2", "c3"]
    v = webfiles.view(st, {}, NOW)["roots"]
    assert [(x["where"], x["path"], x["key"], x["pods"]) for x in v] == [
        ("shop-php-fpm, shop-worker", "/srv/shop", "c1,c2", 1), ("shop-nginx", "/srv/shop/backend", "c3", 1)]
    assert [f["p"] for f in v[0]["php"]] == ["backend/web/uploads/a.php"]
    assert webfiles.problems(st) == [(2, "похоже на взлом: php в загрузках в /srv/shop (shop-php-fpm, shop-worker): a.php",
                                      "php:shop-php-fpm, shop-worker:/srv/shop:backend/web/uploads/a.php")]


# ---------- WordPress обновляет себя сам ----------

WP_ROOT = {**ROOT, "wp": "6.6.2"}
LANG = "wp-content/languages/plugins/wordpress-seo-ru_RU.l10n.php"


def wp_start():
    return start((WP_ROOT,))


def test_language_pack_is_wordpress_not_an_edit():
    # пакет перевода: .l10n.php и .mo в одну секунду - серая строка обновления, не "правка"
    st = webfiles.process(wp_start(), blk(run(T - 600, n_changed=1, changed=[[LANG, T - 700, 52000, T - 700]]),
                                          roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "deploy" not in r and "php" not in r
    assert r["wpev"] == [{"k": "lang", "part": "plugin", "slug": "wordpress-seo", "loc": "ru_RU",
                          "at": datetime.fromtimestamp(T - 700, timezone.utc).isoformat()}]
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


def test_plugin_update_is_a_gray_row():
    up = ["plugin", "hello-dolly", "1.7.2", "1.7.3", "wp-content/plugins/hello-dolly/"]
    st = webfiles.process(wp_start(), blk(run(T - 600, n_changed=2, wp=[up], changed=[
        ["wp-content/plugins/hello-dolly/hello.php", T - 700, 2500],
        ["wp-content/plugins/hello-dolly/readme.js", T - 699, 300]]), roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "deploy" not in r and "php" not in r
    assert r["wpev"][0] == {"k": "plugin", "slug": "hello-dolly", "from": "1.7.2", "to": "1.7.3",
                            "at": datetime.fromtimestamp(T - 700, timezone.utc).isoformat()}
    # большое обновление: в списке 15 файлов из 400, все его - ни выкладки, ни правки
    many = [[f"wp-content/plugins/wordpress-seo/src/f{i:02}.php", T - 500, 100] for i in range(15)]
    st = webfiles.process(st, blk(run(T - 450, n_changed=400, changed=many, wp=[
        ["plugin", "wordpress-seo", "23.5", "23.6", "wp-content/plugins/wordpress-seo/"]]), roots=(WP_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "deploy" not in r and r["wpev"][0]["to"] == "23.6"


# ---------- сверка с выпуском на wordpress.org ----------

OFFICIAL = {("core", "6.9.10", "ru_RU"): {"wp-includes/user.php": ["a" * 32], "wp-login.php": ["b" * 32]},
            ("plugin:akismet", "5.3", ""): {"akismet.php": ["c" * 32], "class.akismet.php": ["d" * 32]},
            ("plugin:akismet", "5.4", ""): {"akismet.php": ["e" * 32], "class.akismet.php": ["f" * 32]},
            ("plugin:wp-rocket", "3.10", ""): {}}
WP9 = {**ROOT, "wp": "6.9.10", "wploc": "ru_RU"}
AK = "wp-content/plugins/akismet/akismet.php"


def sums_of(d):
    def get(ref, ver, loc):
        return d.get((ref, ver, loc if ref == "core" else ""))
    return get


def wp_run(ts, files, sums, **kw):
    return run(ts, n_changed=len(files), changed=[[p, ts - 50, 900] for p in files],
               sums={p: list(v) for p, v in sums.items()}, **kw)


def test_wordpress_file_as_in_release_is_not_a_sign():
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 600, ["wp-includes/user.php", AK], {
        "wp-includes/user.php": ["a" * 32, "core", "wp-includes/user.php", "6.9.10"],
        AK: ["c" * 32, "plugin:akismet", "akismet.php", "5.3"]}), roots=(WP9,)), NOW)
    assert len(st["roots"]["2080-11"]["wpchk"]) == 2
    assert webfiles.view(st, {}, NOW)["roots"][0]["checking"] == 2
    st = webfiles.verify(st, NOW, sums_of(OFFICIAL))
    assert "wpchk" not in st["roots"]["2080-11"] and "php" not in st["roots"]["2080-11"]


def test_wordpress_file_not_as_in_release_is_a_sign_after_next_run():
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 600, ["wp-includes/user.php"], {
        "wp-includes/user.php": ["0" * 32, "core", "wp-includes/user.php", "6.9.10"]}, f={
        "wp-includes/user.php": ["", "www-data", 1, 0, "", 0]}), roots=(WP9,)), NOW)
    # в том же прогоне ждем: обновление, которое шло во время прогона, к следующему закончится
    st = webfiles.verify(st, NOW, sums_of(OFFICIAL))
    assert "php" not in st["roots"]["2080-11"] and "wp-includes/user.php" in st["roots"]["2080-11"]["wpchk"]
    st = webfiles.verify(webfiles.process(st, blk(run(T - 300, n_changed=0), roots=(WP9,)), NOW), NOW,
                         sums_of(OFFICIAL))
    sign = st["roots"]["2080-11"]["php"]["wp-includes/user.php"]
    assert sign["y"] == "wporg" and sign["ver"] == "6.9.10" and sign["ref"] == "core" and sign["o"] == "www-data"
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st)[0][1] == ("похоже на взлом: файлы ядра WordPress не как в выпуске 6.9.10 в "
                                           "/var/www/html (wordpress-7d9f): user.php, владелец www-data")
    assert webfiles.tg_short(webfiles.problems(st))[0] == "Похоже на взлом: файлы ядра WordPress не как в выпуске 6.9.10"
    # файл вернули из выпуска: снова совпал - признак снимается сам
    st = webfiles.process(st, blk(wp_run(T - 100, ["wp-includes/user.php"], {
        "wp-includes/user.php": ["a" * 32, "core", "wp-includes/user.php", "6.9.10"]}), roots=(WP9,)), NOW)
    st = webfiles.verify(st, NOW, sums_of(OFFICIAL))
    r = st["roots"]["2080-11"]
    assert "php" not in r and r["cleared"]["why"] == "wporg"


def test_update_during_a_run_is_not_a_sign():
    # прогон попал в середину обновления akismet 5.3 -> 5.4: файл уже новый, заголовок еще старый
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 900, [AK], {
        AK: ["e" * 32, "plugin:akismet", "akismet.php", "5.3"]}), roots=(WP9,)), NOW)
    # к следующему прогону версия сменилась: файл сверяется с новой (и прошлой) версией
    st = webfiles.process(st, blk(run(T - 600, wp=[["plugin", "akismet", "5.3", "5.4", "wp-content/plugins/akismet/"]]),
                                  roots=(WP9,)), NOW)
    assert st["roots"]["2080-11"]["wpchk"][AK]["ver"] == "5.4"
    st = webfiles.verify(st, NOW, sums_of(OFFICIAL))
    assert "php" not in st["roots"]["2080-11"] and "wpchk" not in st["roots"]["2080-11"]


def test_extra_php_in_core_and_files_without_release():
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 900, [
        "wp-includes/class-wp-cache-loader.php", "wp-content/plugins/akismet/cache/x.php", "wp-content/plugins/wp-rocket/inc/a.php",
        "wp-content/plugins/akismet/views/new.js"], {
        "wp-includes/class-wp-cache-loader.php": ["0" * 32, "core", "wp-includes/class-wp-cache-loader.php", "6.9.10"],
        "wp-content/plugins/akismet/cache/x.php": ["0" * 32, "plugin:akismet", "cache/x.php", "5.3"],
        "wp-content/plugins/wp-rocket/inc/a.php": ["0" * 32, "plugin:wp-rocket", "inc/a.php", "3.10"],
        "wp-content/plugins/akismet/views/new.js": ["0" * 32, "plugin:akismet", "views/new.js", "5.3"]}),
        roots=(WP9,)), NOW)
    st = webfiles.verify(webfiles.process(st, blk(run(T - 600, n_changed=0), roots=(WP9,)), NOW), NOW, sums_of(OFFICIAL))
    r = st["roots"]["2080-11"]
    # лишний php в ядре - признак; своего файла плагина нет в выпуске (кэш), у платного плагина
    # эталона нет - не сверить, не признак
    assert {p: v["y"] for p, v in r["php"].items()} == {"wp-includes/class-wp-cache-loader.php": "wpextra"}
    assert "wpchk" not in r


def test_without_network_checks_wait_a_day_and_never_turn_red():
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 900, [AK], {AK: ["0" * 32, "plugin:akismet", "akismet.php", "5.3"]}),
                                             roots=(WP9,)), NOW)
    st = webfiles.process(st, blk(run(T - 600, n_changed=0), roots=(WP9,)), NOW)
    st = webfiles.verify(st, NOW, sums_of({}))
    assert "php" not in st["roots"]["2080-11"] and AK in st["roots"]["2080-11"]["wpchk"]
    st = webfiles.verify(st, NOW + timedelta(days=2), sums_of({}))
    assert "wpchk" not in st["roots"]["2080-11"] and "php" not in st["roots"]["2080-11"]
    # что загрузить: эталон версии файла, у ядра - по локали
    st = webfiles.process(start((WP9,)), blk(wp_run(T - 900, ["wp-login.php"], {
        "wp-login.php": ["0" * 32, "core", "wp-login.php", "6.9.10"]}), roots=(WP9,)), NOW)
    webfiles._SUMS.clear()  # noqa: SLF001
    assert webfiles._want(st["roots"]) == [("core", "6.9.10", "ru_RU")]  # noqa: SLF001


def test_wordpress_sums_from_wordpress_org():
    core = {"checksums": {"wp-includes/user.php": "a" * 32}}
    plug = {"plugin": "akismet", "files": {"akismet.php": {"md5": "c" * 32, "sha256": "x"},
                                           "readme.txt": {"md5": ["1" * 32, "2" * 32]}}}
    assert webfiles._sums_parse("core", core) == {"wp-includes/user.php": ["a" * 32]}  # noqa: SLF001
    assert webfiles._sums_parse("plugin:akismet", plug) == {"akismet.php": ["c" * 32], "readme.txt": ["1" * 32, "2" * 32]}  # noqa: SLF001
    assert webfiles._sums_parse("core", {"checksums": False}) == {}  # noqa: SLF001
    assert webfiles._sums_url("core", "6.9.10", "ru_RU").endswith("version=6.9.10&locale=ru_RU")  # noqa: SLF001
    assert webfiles._sums_url("plugin:akismet", "5.3", "") == "https://downloads.wordpress.org/plugin-checksums/akismet/5.3.json"  # noqa: SLF001
    # кривое из helper'а не принимается: не md5, путь наверх, чужая ссылка
    assert webfiles._sums({"sums": {"a.php": ["x", "core", "a.php", "1"], "b.php": ["0" * 32, "core", "../b.php", "1"],  # noqa: SLF001
                                    "c.php": ["0" * 32, "theme:x", "c.php", "1"], "d.php": ["0" * 32, "core", "d.php", "1"]}}) == {
        "d.php": {"m": "0" * 32, "ref": "core", "rel": "d.php", "ver": "1"}}


# ---------- рабочая копия git, файлы плагина, перезапись тем же содержимым ----------

GIT_ROOT = {**ROOT, "where": "host", "path": "/srv/api", "branch": "master"}
SRC = "src/Service/Report/Column.php"


def test_same_content_as_head_is_a_quiet_row():
    st = webfiles.process(start((GIT_ROOT,)), blk(run(T - 600, n_changed=1, changed=[[SRC, T - 700, 900]], git_same=[SRC]),
                                                  roots=(GIT_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "deploy" not in r and r["same"]["files"] == [SRC]
    v = webfiles.view(st, {}, NOW)["roots"][0]
    assert v["branch"] == "master" and v["same"]["n"] == 1


def test_git_does_not_hide_break_in_but_committed_code_is_no_evidence():
    # php в загрузках вне git - улика; тот же файл, что в HEAD, - код из репозитория, не улика
    st = webfiles.process(start((GIT_ROOT,)), blk(run(T - 600, n_new=2, new=[
        ["public/uploads/shell.php", T - 700, 900], ["public/uploads/thumb.php", T - 700, 300]],
        f={"public/uploads/shell.php": ["new", "www-data", 1, 1], "public/uploads/thumb.php": ["same", "deploy", 0, 0]}),
        roots=(GIT_ROOT,)), NOW)
    r = st["roots"]["2080-11"]
    assert list(r["php"]) == ["public/uploads/shell.php"] and r["php"]["public/uploads/shell.php"]["y"] == "shell"


def test_hanging_sign_clears_when_file_is_gone_or_back():
    st = webfiles.process(start(), blk(run(T - 900, n_new=2, new=[
        ["wp-content/uploads/a.php", T - 950, 300], ["wp-content/uploads/b.php", T - 950, 300]])), NOW)
    clean = [["wp-content/uploads/a.php", "head"], ["wp-content/uploads/b.php", "data"], ["zz.php", "odd"]]
    st = webfiles.process(st, blk(roots=({**ROOT, "clean": clean},)), NOW)
    # php в загрузках "как в git" или "настройками" не оправдать: снимается, только когда его нет
    assert sorted(st["roots"]["2080-11"]["php"]) == ["wp-content/uploads/a.php", "wp-content/uploads/b.php"]
    st = webfiles.process(st, blk(roots=({**ROOT, "clean": [["wp-content/uploads/a.php", "gone"],
                                                            ["wp-content/uploads/b.php", "back"]]},)), NOW)
    r = st["roots"]["2080-11"]
    assert "php" not in r and r["cleared"]["why"] in ("gone", "back")
    # тот же досмотр в следующих блоках (helper повторяет его сутки) ничего не ломает
    again = webfiles.process(st, blk(roots=({**ROOT, "clean": [["wp-content/uploads/a.php", "gone"]]},)), NOW)
    assert again["roots"] == st["roots"]


def test_rewrite_with_same_content_is_a_quiet_row():
    st = webfiles.process(start(), blk(run(T - 600, n_same=3, same=[
        ["wp-content/autoptimize_404_handler.php", T - 650, 2213], ["config/main-local.php", T - 640, 236],
        ["config/params-local.php", T - 640, 99]])), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "deploy" not in r and r["same"]["n"] == 3 and r["same"]["git"] is False


def test_file_details_owner_and_exec():
    ht, ini = "wp-content/uploads/.htaccess", ".user.ini"
    st = webfiles.process(start(), blk(run(T - 600, n_new=1, n_changed=2, new=[[ht, T - 650, 30]],
        changed=[[ini, T - 640, 50], ["config/main-local.php", T - 640, 240]],
        f={ht: ["", "www-data", 1, 0, "", 1], ini: ["", "root", 0, 0, "", 0],
           "config/main-local.php": ["ignored", "gitlab-runner", 0, 0]})), NOW)
    r = st["roots"]["2080-11"]
    assert r["php"] == {ht: {"since": r["php"][ht]["since"], "s": 30, "t": T - 650, "k": "new", "y": "exec", "o": "www-data",
                             "w": 1}}
    assert r["edits"]["o"] == ["gitlab-runner", "root"] and r["edits"]["files"] == [".user.ini", "config/main-local.php"]
    st["loose"] = ["2080-11"]
    assert webfiles.problems(st)[0][1] == "похоже на взлом: .htaccess запускает php в /var/www/html (wordpress-7d9f): .htaccess, владелец www-data (веб-сервер)"
    assert webfiles._info({"f": {"a.php": ["", "root", 0, 0, "plugin:x y"]}}) == {"a.php": {"o": "root"}}  # noqa: SLF001


DROPIN = "wp-content/advanced-cache.php"


def test_plugin_files_are_a_quiet_row():
    conf, closed = "wp-content/wp-rocket-config/shop.example.com.php", "wp-content/uploads/wpforms/.htaccess"
    st = webfiles.process(start(), blk(run(T - 600, n_changed=1, n_new=2, changed=[[DROPIN, T - 650, 2452]],
        new=[[conf, T - 640, 1928], [closed, T - 640, 99]],
        f={DROPIN: ["", "root", 0, 0, "plugin:wp-rocket"], conf: ["", "www-data", 1, 0, "data"],
           closed: ["", "www-data", 1, 0, "deny"]})), NOW)
    r = st["roots"]["2080-11"]
    assert "edits" not in r and "php" not in r and "deploy" not in r
    assert r["gen"]["n"] == 3 and r["gen"]["by"] == ["data", "deny", "plugin:wp-rocket"]


def test_setup_version_lines_are_well_formed():
    # в 1.8.9 у webfiles-setup пропал пробел перед "#": bash взял "0.5#" версией, остаток строки
    # выполнил командой. Строка версии каждого helper'а - число, пробел и комментарий или конец
    agent = Path(__file__).resolve().parents[2] / "agent"
    for f in sorted(agent.glob("*-setup.sh")):
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("KERVAX_SETUP_VERSION=")]
        assert len(lines) == 1 and re.fullmatch(r"KERVAX_SETUP_VERSION=\d+\.\d+(?:[ \t]+#.*)?", lines[0]), (f.name, lines)


def test_helper_06_content_and_watch():
    src = SRC_FILE.read_text(encoding="utf-8")
    # хеш каждого файла в списке, перезапись тем же содержимым - "S", досмотр висящих 30 дней
    assert "sha1sum" in src and '"S" : "C"' in src and "WATCH_DAYS=30" in src and '\\"clean\\":[$clean]' in src
    # git: только ls-tree и check-ignore без индекса, без глобальных исключений, без fsmonitor
    assert "check-ignore --no-index" in src and "core.fsmonitor=" in src and "core.excludesFile=/dev/null" in src
    assert "git status" not in src and "git diff" not in src and "ls-files" not in src
    # sha1 с ноды не уходят, только вердикты (md5 - лишь у файлов ядра и плагинов WordPress)
    assert '\\"h\\"' not in src
    assert "SEED_DAYS=14" in src and "c[$2] < 5" in src and 'head -n "$WATCH_MAX"' in src


# ---------- awk helper'а: файлы плагина, запуск php, каталоги загрузок ----------

def _block(name: str) -> str:
    src = SRC_FILE.read_text(encoding="utf-8")
    if name == "UP_AWK":
        return re.search(r"UP_AWK='(.*?)'\n", src, re.S).group(1)
    return re.search(rf"<<'{name}_EOF' \|\| true\n(.*?){name}_EOF\n", src, re.S).group(1)


def _awk(tmp_path: Path, block: str, args: list[str], *texts: str) -> subprocess.CompletedProcess:
    awk = shutil.which("awk")
    if not awk:
        pytest.skip("нет awk")
    prog = tmp_path / "p.awk"
    prog.write_text(_block(block), encoding="utf-8", newline="\n")
    files = []
    for i, t in enumerate(texts):
        f = tmp_path / f"f{i}"
        f.write_text(t, encoding="utf-8", newline="\n")
        files.append(f.as_posix())
    return subprocess.run([awk, *args, "-f", prog.as_posix(), *files], capture_output=True, timeout=30,
                          env={"LC_ALL": "C"}, text=True)


def _gen(tmp_path: Path, mode: str, *texts: str) -> bool:
    return _awk(tmp_path, "GEN_AWK", ["-v", f"mode={mode}", "-v", "base=/var/www/html"], *texts).returncode == 0


def _exec(tmp_path: Path, text: str, up: int = 0) -> bool:
    return _awk(tmp_path, "EXEC_AWK", ["-v", f"up={up}"], text).returncode == 0


def test_helper_tells_php_run_where_it_must_not(tmp_path):
    # картинки и текст как php, php для каталога загрузок или картинок в <Files>, подмешанный файл
    assert _exec(tmp_path, "AddHandler application/x-httpd-php .jpg\n")
    assert _exec(tmp_path, "AddType application/x-httpd-php .php .png\n")
    assert _exec(tmp_path, "SetHandler application/x-httpd-php\n", up=1)
    assert _exec(tmp_path, '<FilesMatch "\\.(jpg|png)$">\n  SetHandler application/x-httpd-php\n</FilesMatch>\n')
    assert _exec(tmp_path, "php_flag engine on\n", up=1)
    assert _exec(tmp_path, "php_value auto_prepend_file /var/www/html/wp-content/uploads/2026/x.php\n")
    assert _exec(tmp_path, "auto_prepend_file = '/tmp/.x'\n")
    # обычные настройки хостинга и плагинов - нет: выбор версии PHP, php-fpm для .php, Wordfence,
    # типы файлов, запрет php в загрузках, обработчик по умолчанию
    for ok in ("AddHandler application/x-httpd-ea-php74 .php .php7 .phtml\n", "AddType image/webp .webp\n",
               "AddType application/octet-stream .wpress\n", "SetHandler default-handler\nForceType none\n",
               '<FilesMatch "\\.php$">\n  SetHandler "proxy:unix:/run/php/php8.2-fpm.sock|fcgi://localhost"\n</FilesMatch>\n',
               "auto_prepend_file = '/var/www/html/wordfence-waf.php'\n",
               "php_value auto_prepend_file none\n", "php_flag engine off\n", "# AddHandler application/x-httpd-php .jpg\n"):
        assert not _exec(tmp_path, ok), ok
    assert not _exec(tmp_path, "php_flag engine on\n")  # вне загрузок - обычная настройка


def test_helper_uploads_match_the_panel(tmp_path):
    paths = ["wp-content/uploads/2026/10/x.php", "public/uploads/x.php", "upload/iblock/x.php", "web/tmp/a.php",
             "backend/web/uploads/a.php", "sites/default/files/x.php", "src/Web/Upload/Handler.php",
             "wp-includes/php-ai-client/src/Files/DTO/File.php", "src/Media/Image.php", "vendor/x/files/a.php",
             "uploads.php", "wp-content/plugins/x/upload/a.php"]
    got = _awk(tmp_path, "UP_AWK", [], "\n".join(paths) + "\n").stdout.split()
    assert got == [p for p in paths if webfiles.in_uploads(p)]
    assert len(got) == 6


TPL = """<?php
defined( 'ABSPATH' ) || exit;

$rocket_path        = '{{WP_ROCKET_PATH}}';
if ( version_compare( phpversion(), '{{WP_ROCKET_PHP_VERSION}}', '<' ) ) {
\treturn;
}
'{{MOBILE_CACHE}}';
include_once '{{WP_ROCKET_PATH}}inc/Mobile_Detect.php';
'{{/MOBILE_CACHE}}';
require $rocket_path . 'inc/class-cache.php';
"""
DROP = """<?php
defined( 'ABSPATH' ) || exit;
$rocket_path        = '/var/www/html/wp-content/plugins/wp-rocket/';
if ( version_compare( phpversion(), '7.0', '<' ) ) {
\treturn;
}

require $rocket_path . 'inc/class-cache.php';
"""
REQ = "require $rocket_path . 'inc/class-cache.php';\n"


def test_helper_tells_dropin_from_its_template(tmp_path):
    assert _gen(tmp_path, "tpl", TPL, DROP)
    # строка кода, которой нет в шаблоне, - не его
    assert not _gen(tmp_path, "tpl", TPL, DROP.replace("\nrequire", "\n@include '/var/www/html/x.ico';\nrequire"))
    # строки шаблона не в том порядке - тоже
    assert not _gen(tmp_path, "tpl", TPL, DROP.replace(REQ, "").replace("<?php\n", "<?php\n" + REQ))
    # путь вне сайта или наверх через .., код вместо версии
    assert not _gen(tmp_path, "tpl", TPL, DROP.replace("/var/www/html/wp-content/plugins/wp-rocket/", "/tmp/x/"))
    assert not _gen(tmp_path, "tpl", TPL, DROP.replace("/var/www/html/wp-content/plugins/wp-rocket/", "../../../tmp/"))
    assert not _gen(tmp_path, "tpl", TPL, DROP.replace("'7.0'", "'7.0' . system('id') . ''"))
    # конфиг WP Super Cache из -sample: другое значение и новая настройка - можно, вызов - нет
    sample = ("<?php\nif ( ! defined('WPCACHEHOME') )\n\tdefine( 'WPCACHEHOME', WP_PLUGIN_DIR . '/wp-super-cache/' );\n"
              "$cache_enabled = false;\n")
    cfg = ("<?php\n$cache_page_secret = 'abc';\nif ( ! defined('WPCACHEHOME') )\n"
           "\tdefine( 'WPCACHEHOME', WP_CONTENT_DIR . '/plugins/wp-super-cache/' );\n$cache_enabled = true;\n")
    assert _gen(tmp_path, "tpl", sample, cfg)
    assert not _gen(tmp_path, "tpl", sample, cfg.replace("true;", "'system'('id');"))
    assert not _gen(tmp_path, "tpl", sample, cfg.replace("true;", "`id`;"))


def test_helper_tells_data_and_closed_dir(tmp_path):
    conf = ("<?php\ndefined( 'ABSPATH' ) || exit;\n$rocket_cookie_hash = 'c0ffee';\n$q = array (\n  'utm_source' => 1,\n);\n"
            "define( 'X', true );\n")
    assert _gen(tmp_path, "data", conf)
    assert _gen(tmp_path, "data", "<?php exit('Access denied'); __halt_compiler(); ?>\nbinary")
    assert _gen(tmp_path, "data", '<?php exit; ?>{"pgcache.enabled":true,"dir":"\\/x"}')
    for bad in ("$a = 'system'('id');", "$a = ['system'][0]('id');", '$a = "x{$_GET[1]}";', "$a = `id`;",
                "// ?> <?php system('id');", "$a = array(1)(2);", "eval($_POST[1]);"):
        assert not _gen(tmp_path, "data", conf + bad + "\n"), bad
    # строка exit с \ перед кавычкой: для php строка идет дальше и вызов перед exit выполнился бы
    assert not _gen(tmp_path, "data", "<?php exit('\\');X' . system('id'));\n")
    closed = ("<IfModule mod_authz_core.c>\n\tRequire all denied\n</IfModule>\n<IfModule !mod_authz_core.c>\n"
              "\tOrder deny,allow\n\tDeny from all\n</IfModule>")
    assert _gen(tmp_path, "deny", closed)
    assert not _gen(tmp_path, "deny", "Deny from all\nAddHandler application/x-httpd-php .jpg\n")
    assert not _gen(tmp_path, "deny", "Options -Indexes\n")


def test_helper_07_plugin_files():
    src = SRC_FILE.read_text(encoding="utf-8")
    assert '-name "${b%.php}-sample.php"' in src and "v=${GEN[$f]}" in src
    assert '[ -z "${sus[$f]:-}" ]' in src and "mode=tpl" in src and "mode=data" in src and "mode=deny" in src
