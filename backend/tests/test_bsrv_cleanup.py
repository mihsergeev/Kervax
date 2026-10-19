"""Вкладка "Чистка" сервера бэкапов (app/bsrv_cleanup.py): у каждого пункта готовые шаги под этот
сервер и эти репозитории, две причины про один репозиторий - одна строка."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app import bsrv_cleanup, collector

DAY = 86400
POL = {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
MISSING = ["web-old-1", "web-old-2", "db-old-1"]


def _server(now: datetime, helper="0.33", where="", archive=None, missing=None, mutes=None):
    ts = int(now.timestamp())
    repos = [
        # чистил старый скрипт, а его сняли с cron: снапшоты копятся с лета
        dict(POL, name="repo-a", snapshots=126, oldest_snapshot=ts - 261 * DAY, last_activity=ts - 14 * 3600),
        # замороженный проект на том же скрипте, в архиве
        dict(POL, name="proj-a", snapshots=14, oldest_snapshot=ts - 260 * DAY, last_activity=ts - 110 * DAY),
        # своя чистка идет, но держится группа прежнего хоста
        dict(POL, name="repo-b", snapshots=28, oldest_snapshot=ts - 870 * DAY, last_activity=ts - 6 * 3600,
             rotation_ts=ts - 5 * 3600, rotation_ok=1, rotation_removed=1),
        # своя чистка не открывает репозиторий
        dict(POL, name="repo-c", snapshots=10, oldest_snapshot=ts - 20 * DAY, last_activity=ts - 6 * 3600,
             rotation_ts=ts - 5 * 3600, rotation_ok=0, rotation_removed=-1),
    ]
    obs = {
        "repo-a": {"cleaner": "legacy", "removed_ts": 0, "seen_since": ts - 6 * DAY - 3600},
        "proj-a": {"cleaner": "legacy", "removed_ts": 0, "seen_since": ts - 6 * DAY},
        "repo-b": {"cleaner": "script", "removed_ts": ts - 5 * 3600, "seen_since": ts - 60 * DAY,
                   "groups": {"ts": ts - 5 * 3600, "groups": [
                       {"host": "repo-b", "tags": [], "paths": ["/"], "n": 14, "first": ts - 140 * DAY,
                        "last": ts - 6 * 3600},
                       {"host": "repo-b-old", "tags": [], "paths": ["/"], "n": 14, "first": ts - 870 * DAY,
                        "last": ts - 735 * DAY}]}},
        "repo-c": {"cleaner": "script", "removed_ts": 0, "seen_since": ts - 60 * DAY,
                   "prune_err": "Fatal: wrong password or no key found"},
    }
    legacy = {"script": "/etc/systemd-rest.conf", "repos": 5, "where": where, "schedule": "30 16 * * *" if where else "",
              "log": "", "log_ts": 0, "missing": MISSING if missing is None else missing}
    return SimpleNamespace(
        name="backup-a",
        last_report={
            "clock_unix": ts,
            "setup_versions": {"backupserver-setup": helper},
            "backup_server": {"present": True, "repos": repos},
            "extras": {"backup-server": {"v": 1, "ts": ts - 60, "legacy": legacy, "repos": obs}},
        },
        alert_state={},
        backup_repo_mutes=mutes or [],
        backup_repo_archive={"proj-a": {"note": "проект заморожен"}} if archive is None else archive,
    )


def _ids(steps):
    return [x["id"] for x in steps]


def test_one_line_per_repo_with_both_reasons():
    """Старье пережило политику и удалений нет - одна беда одного репозитория: одна строка с обеими
    причинами (раньше в "Чистке" и на главной шли две)."""
    now = datetime.now(timezone.utc)
    s = _server(now)
    why = collector.rotation_reasons(s, now)
    assert why["repo-a"] == ["старейшему снапшоту 261 дн. при политике до 231",
                             "126 снапшотов при политике 17, удалений нет 6 дн"], why
    items = collector.backup_rotation_items(s, now)
    assert len([x for x in items if x.startswith("repo-a ")]) == 1, items
    assert ("repo-a (старейшему снапшоту 261 дн. при политике до 231, 126 снапшотов при политике 17, "
            "удалений нет 6 дн)") in items
    assert collector.merge_rotation(["x (a)", "x (b)", "y (c)", "x (a)"]) == ["x (a, b)", "y (c)"]


def test_legacy_not_scheduled_lists_live_repos_and_moves_them():
    """Старый скрипт не запущен: в плашке только живые репозитории (архив отдельно числом), шаги -
    пробный перенос, перенос, проверка, первая чистка, откат."""
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now), now)
    leg = v["legacy"]
    assert [r["name"] for r in leg["repos"]] == ["repo-a"] and leg["archived"] == 1 and not leg["scheduled"]
    assert len(leg["repos"][0]["why"]) == 2 and leg["repos"][0]["snaps"] == 126
    fx = leg["fix"]
    assert _ids(fx) == ["adopt_dry", "adopt_apply", "adopt_check", "adopt_run", "adopt_undo"]
    assert fx[0]["cmd"] == "sudo /lib65/kervax/kervax-backupserver-helper adopt-legacy repo-a"
    assert fx[1]["cmd"].endswith("adopt-legacy --apply repo-a")
    assert "/etc/cron.d/kervax-prune-repo-a" in fx[2]["cmd"]
    assert "restic-prune-repo-a.sh" in fx[3]["cmd"]
    assert fx[4].get("undo") and "kervax-bak-" in fx[4]["cmd"] and "/app/rest-server/system/envs/repo-a.env" in fx[4]["cmd"]
    # в "Чистке" тот же репозиторий: старый скрипт не запускается, шаги те же
    rot = {x["repo"]: x for x in v["rotation"]}
    assert rot["repo-a"]["cause"] == "legacy_off" and _ids(rot["repo-a"]["fix"])[0] == "adopt_dry"
    assert "proj-a" not in rot


def test_legacy_all_archived_is_not_a_problem():
    """На старом скрипте остались только репозитории из архива: плашки нет."""
    now = datetime.now(timezone.utc)
    arch = {"proj-a": {}, "repo-a": {}}
    assert bsrv_cleanup.view(_server(now, archive=arch), now)["legacy"] is None


def test_dead_blocks_are_a_list_with_steps():
    """Блоки на удаленные репозитории - список имен и команда, которая закомментирует именно их, с
    копией файла и откатом. Имя, которое нельзя подставить в команду, пропускаем."""
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now, missing=MISSING + ["bad;name"]), now)
    d = v["dead"]
    assert d["repos"] == MISSING and not d["scheduled"]
    assert _ids(d["fix"]) == ["dead_dry", "dead_apply", "dead_check", "dead_undo"]
    assert d["fix"][0]["cmd"].endswith("adopt-legacy web-old-1 web-old-2 db-old-1")
    assert "--apply web-old-1 web-old-2 db-old-1" in d["fix"][1]["cmd"]
    assert d["fix"][2]["arg"] == "3" and "sh -n /etc/systemd-rest.conf" in d["fix"][2]["cmd"]
    assert d["fix"][3]["undo"] and "bad;name" not in str(v)
    # скрипт в cron: блоки шумят каждый день
    assert bsrv_cleanup.view(_server(now, where="crontab root"), now)["dead"]["scheduled"]


def test_scheduled_legacy_repo_is_its_own_cause():
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now, where="crontab root"), now)
    assert v["legacy"]["scheduled"]
    assert {x["repo"]: x["cause"] for x in v["rotation"]}["repo-a"] == "legacy"


def test_old_group_steps_forget_and_merge():
    """Старая группа: удалить (сначала без --apply) или, если это прежнее имя сервера, склеить.
    Склейка - с helper'а 0.33, раньше - шаг "обновите helper"."""
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now), now)
    rot = {x["repo"]: x for x in v["rotation"]}
    assert rot["repo-b"]["cause"] == "old_group" and rot["repo-b"]["hosts"] == ["repo-b-old"]
    g = v["groups"]["repo-b"]["repo-b-old"]
    assert _ids(g["forget"]) == ["group_dry", "group_apply", "group_check", "group_keep"]
    assert g["forget"][0]["cmd"] == "sudo /lib65/kervax/kervax-backupserver-helper forget-group repo-b repo-b-old"
    assert g["forget"][1]["cmd"].endswith("--apply") and g["forget"][3]["alt"]
    # пути и теги те же, а новая группа началась позже конца старой больше чем на неделю - не
    # переименование
    assert "merge" not in g
    s = _server(now)
    grp = s.last_report["extras"]["backup-server"]["repos"]["repo-b"]["groups"]["groups"]
    grp[0]["first"] = grp[1]["last"] + 3600
    g = bsrv_cleanup.view(s, now)["groups"]["repo-b"]["repo-b-old"]
    assert _ids(g["merge"]) == ["merge_dry", "merge_apply", "group_check"]
    assert g["merge"][0]["cmd"].endswith("merge-group repo-b repo-b-old repo-b") and g["merge"][0]["arg"] == "repo-b"
    s = _server(now, helper="0.32")
    s.last_report["extras"]["backup-server"]["repos"]["repo-b"]["groups"]["groups"][0]["first"] = grp[1]["last"] + 3600
    g = bsrv_cleanup.view(s, now)["groups"]["repo-b"]["repo-b-old"]
    assert g["merge"] == [{"id": "helper_update", "arg": "0.33"}] and g["forget"][0]["id"] == "group_dry"


def test_old_helper_gets_update_step():
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now, helper="0.24"), now)
    assert v["legacy"]["fix"] == [{"id": "helper_update", "arg": "0.25"}]
    assert v["dead"]["fix"] == [{"id": "helper_update", "arg": "0.25"}]


def test_prune_problem_steps_by_kind():
    now = datetime.now(timezone.utc)
    v = bsrv_cleanup.view(_server(now), now)
    pr = {x["repo"]: x for x in v["prune"]}
    assert pr["repo-c"]["kind"] == "access" and _ids(pr["repo-c"]["fix"]) == ["prune_log", "prune_env", "prune_run"]
    assert "sudoedit /app/rest-server/system/envs/repo-c.env" == pr["repo-c"]["fix"][1]["cmd"]
    assert _ids(bsrv_cleanup.prune_fix("repo-c", "failed")) == ["prune_log", "prune_run"]
    assert _ids(bsrv_cleanup.prune_fix("repo-c", "skipped")) == ["prune_policy", "prune_run"]
    assert _ids(bsrv_cleanup.prune_fix("repo-c", "idle")) == ["prune_cron", "prune_run"]
    # заглушенный репозиторий - не пункт, как в алерте
    v = bsrv_cleanup.view(_server(now, mutes=["repo-c"]), now)
    assert not v["prune"]


def test_script_cause_steps_use_its_policy():
    """Своя чистка идет, а старье не уходит и старой группы нет: лог, forget --dry-run с политикой
    репозитория без блокировки, запуск."""
    fx = bsrv_cleanup.script_fix("repo-d", dict(POL, keep_last=3))
    assert _ids(fx) == ["script_log", "script_dry", "script_run"]
    assert "--dry-run --no-lock --group-by host,tags --keep-last 3 --keep-daily 7 --keep-weekly 4 --keep-monthly 6" in fx[1]["cmd"]
    assert ". /app/rest-server/system/envs/repo-d.env" in fx[1]["cmd"]


def test_not_a_backup_server():
    now = datetime.now(timezone.utc)
    s = SimpleNamespace(last_report={"backup_server": {"present": False}}, alert_state={}, backup_repo_mutes=[],
                        backup_repo_archive={})
    assert bsrv_cleanup.view(s, now) is None
