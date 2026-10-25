"""GitLab (app/gitlab.py, helper gitlab-setup): выпуски с Docker Hub и остановки, путь обновления, поводы
алерта "GitLab устарел", заметки про бэкап, мьют, "Что сломано", короткий алерт, раздел, нужен ли helper."""
from datetime import datetime, timezone
from pathlib import Path

from app import collector, gitlab as gl, setup_scripts, srv_short
from app.models import Server

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
DAY = 86400


def _tags(**ages):
    """{"19.4.1": 30} (дней назад) -> теги выборки"""
    return {v.replace("_", "."): TS - d * DAY for v, d in ages.items()}


FEED = {
    "tags": {"ee": _tags(**{"19_2_0": 100, "19_2_9": 2, "19_3_0": 64, "19_3_6": 2, "19_4_0": 36, "19_4_1": 30,
                            "19_4_2": 11, "19_4_3": 2, "19_5_0": 8, "19_5_1": 2})},
    "stops": [[18, 11], [19, 2], [19, 5], [19, 8], [19, 11]],
    "stops_src": "upgrade_path.yml",
}


def _inst(**kw):
    x = {"ct": "gitlab-gitlab-ee-1", "state": "running", "image": "gitlab/gitlab-ee:19.4.1-ee.0", "dir": "/app/gitlab",
         "service": "gitlab-ee", "files": "/app/gitlab/docker-compose.yml", "mode": "image",
         "from": "gitlab/gitlab-ee:19.4.1-ee.0", "dockerfile": "", "version": "19.4.1", "edition": "ee",
         "url": "https://gitlab.example.com", "data": "/app/gitlab/data", "config": "/app/gitlab/config", "secrets": 1,
         "bdir": "/app/gitlab/data/backups", "keep": 0,
         "bk": {"n": 2, "total": 7 * 2**30, "first": TS - 2 * DAY,
                "last": {"name": "x_gitlab_backup.tar", "ts": TS - 3600, "size": 3 * 2**30, "info": 1, "skipped": "",
                         "ver": "19.4.1-ee"}},
         "cfg": {"n": 2, "ts": TS - 3600, "path": "/app/gitlab/data/backups/config/gitlab_config_1.tar"},
         "bbm": {"total": 757, "failed": 0, "open": 0, "list": []},
         "sched": [{"how": "timer", "src": "gitlab-backup.timer", "when": "*-*-* 03:30:00", "db": 1, "skip": "",
                    "etc": 1, "enabled": "enabled", "last": TS - 3600, "next": TS + 20 * 3600, "result": "success"}]}
    x.update(kw)
    return x


def _rep(insts=None, ts=TS, backup=None):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "backup": backup if backup is not None else {"configured": True, "mode": "exclude", "excludes": ["/proc"]},
            "extras": {"gitlab": {"v": 1, "ts": ts, "instances": insts if insts is not None else [_inst()]}}}


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def setup_function():
    gl.set_feed(FEED)


def teardown_function():
    gl.set_feed(None)


def test_parse_hub_tags_and_stops():
    res = [{"name": "19.4.3-ee.0", "tag_last_pushed": "2026-10-21T15:52:46.650117Z"},
           {"name": "19.5.0-rc42.ee.0", "tag_last_pushed": "2026-10-10T00:00:00Z"},
           {"name": "latest", "tag_last_pushed": "2026-10-21T00:00:00Z"},
           {"name": "19.4.3-ce.0", "tag_last_pushed": "2026-10-21T00:00:00Z"}]
    got = gl.parse_tags(res, "ee")
    assert list(got) == ["19.4.3"]
    assert got["19.4.3"] == int(datetime(2026, 10, 21, 15, 52, 46, tzinfo=timezone.utc).timestamp())
    txt = "- major: 18\n  minor: 11\n\n- major: 19\n  minor: 2\n\n- major: 19\n  minor: 5\n"
    assert gl.parse_stops(txt) == [(18, 11), (19, 2), (19, 5)]


def test_status_path_like_autoupgrade():
    st = gl.status(_inst(), FEED, NOW)
    assert st["latest"] == "19.5.1" and st["lp"] == "19.4.3"
    # самый старый не поставленный патч своей минорной и сколько он уже есть
    assert st["patch"] == {"ver": "19.4.2", "newest": "19.4.3", "days": 11}
    # 19.5 - вышедшая остановка, 19.4 ниже нее
    assert st["stop"] == {"ver": "19.5", "days": 8}
    assert st["next_stop"] == "19.5" and st["target"] == "19.5.1" and st["path"] == ["19.5.1"]
    # со старой минорной путь идет через остановку ее последним патчем
    st = gl.status(_inst(version="19.1.3"), {**FEED, "tags": {"ee": {**FEED["tags"]["ee"], "19.1.3": TS - 120 * DAY}}}, NOW)
    assert st["path"] == ["19.2.9", "19.5.1"]
    # на самой остановке сперва ее последний патч
    st = gl.status(_inst(version="19.2.0"), FEED, NOW)
    assert st["path"] == ["19.2.9", "19.5.1"]
    # свежая версия: ни патча, ни остановки
    st = gl.status(_inst(version="19.5.1"), FEED, NOW)
    assert st["patch"] is None and st["stop"] is None and st["path"] == []
    # новая мажорная - отдельно, в путь не входит
    f2 = {**FEED, "tags": {"ee": {**FEED["tags"]["ee"], "20.0.0": TS - DAY}}}
    st = gl.status(_inst(version="19.5.1"), f2, NOW)
    assert st["major"] == "20.0.0" and st["path"] == []
    # версия неизвестна (контейнер лежит) или выпусков нет - судить не по чему
    assert gl.status(_inst(version=""), FEED, NOW) is None
    assert gl.status(_inst(edition="ce"), FEED, NOW) is None


def test_causes_thresholds(monkeypatch):
    monkeypatch.setattr(gl, "PATCH_DAYS", 7)
    monkeypatch.setattr(gl, "STOP_DAYS", 14)
    # патч 11 дней - повод; остановке 8 дней - еще нет
    assert gl.causes(_inst(), FEED, NOW) == ["patch"]
    monkeypatch.setattr(gl, "PATCH_DAYS", 14)
    assert gl.causes(_inst(), FEED, NOW) == []
    monkeypatch.setattr(gl, "STOP_DAYS", 7)
    assert gl.causes(_inst(), FEED, NOW) == ["stop"]
    # автообновление: блокировка версии и "сломан"; сломан важнее
    au = {"blocked": "19.5.1 2026-10-22 23:40:01 после запуска не прошли проверки: readiness: 502", "broken": ""}
    assert "blocked" in gl.causes(_inst(au=au), FEED, NOW)
    au2 = {**au, "broken": "2026-10-22 23:50:00 19.4.1 -> 19.5.1: откат не удался"}
    cs = gl.causes(_inst(au=au2), FEED, NOW)
    assert "broken" in cs and "blocked" not in cs
    # упавшие фоновые миграции
    bbm = {"total": 700, "failed": 1, "open": 1, "list": [{"st": 4, "job": "BackfillSomething", "table": "issues", "col": "id"}]}
    assert "bbm" in gl.causes(_inst(bbm=bbm), FEED, NOW)
    # незавершенные только показываются
    assert "bbm" not in gl.causes(_inst(bbm={"total": 700, "failed": 0, "open": 3, "list": []}), FEED, NOW)


def test_problems_text_mute_and_home(monkeypatch):
    monkeypatch.setattr(gl, "PATCH_DAYS", 7)
    s = _srv()
    pr = gl.problems(s, NOW)
    assert len(pr) == 1 and pr[0]["key"] == "gitlab.example.com" and pr[0]["level"] == 1
    assert "19.4.1, патч 19.4.2 вышел 11 дней назад (последний 19.4.3)" in pr[0]["text"]
    # "Что сломано": раздел gitlab в сервисах, мьют по инстансу
    items = [x for x in collector.server_problems(s, NOW) if x["kind"] == "gitlab"]
    assert len(items) == 1 and items[0]["sec"] == "gitlab" and items[0]["mute"] == "gitlab:gitlab.example.com"
    assert gl.problems(_srv(mutes=["gitlab:gitlab.example.com"]), NOW) == []
    snz = _srv()
    snz.alert_snoozes = {"gitlab:gitlab.example.com": "2026-10-24T12:00:00+00:00"}
    assert gl.problems(snz, NOW) == []
    # протухший блок (helper встал) - судить не по чему
    assert gl.problems(_srv(_rep(ts=TS - 7200)), NOW) == []
    # без выпусков (панель еще не скачала) - только состояние автообновления
    gl.set_feed(None)
    assert gl.problems(s, NOW) == []


def test_tg_short_format_a(monkeypatch):
    monkeypatch.setattr(gl, "PATCH_DAYS", 7)
    what, facts = gl.tg_short(gl.problems(_srv(), NOW))
    assert what == "GitLab устарел"
    assert facts == ["gitlab.example.com: 19.4.1, есть 19.4.3 (патч 11 дней)"]
    monkeypatch.setattr(gl, "STOP_DAYS", 7)
    what, facts = gl.tg_short(gl.problems(_srv(), NOW))
    assert facts == ["gitlab.example.com: 19.4.1, ниже остановки 19.5"]
    bbm = {"total": 700, "failed": 2, "open": 2, "list": [{"st": 4, "job": "BackfillSomething", "table": "t", "col": "c"}]}
    two = _rep([_inst(bbm=bbm), _inst(ct="gitlab-test", url="https://gitlab-test.example.com", version="19.5.1")])
    what, facts = gl.tg_short(gl.problems(_srv(two), NOW))
    assert what == "Упали фоновые миграции GitLab" and facts == ["gitlab.example.com: BackfillSomething и еще 1"]
    au = {"broken": "2026-10-22 23:50:00 19.4.1 -> 19.5.1: откат не удался", "blocked": ""}
    what, _ = gl.tg_short(gl.problems(_srv(_rep([_inst(au=au, version="19.5.1")])), NOW))
    assert what == "GitLab сломан после обновления"
    # алерт целиком: сервер, что случилось, строка конкретики; только символы клавиатуры
    monkeypatch.setattr(gl, "STOP_DAYS", 14)
    what, facts = srv_short.short("gitlab", {"items": gl.problems(_srv(), NOW)}, _srv(), level=1)
    assert what == "GitLab устарел" and srv_short.icon("gitlab") == "⚠️🦊"
    for ch in "—–«»…≥":
        assert ch not in what + "".join(facts)


def test_backup_notes():
    # все хорошо: свежий бэкап по таймеру, конфиг в архиве
    assert gl.backup_notes(_inst(), _rep(), NOW) == []
    # архивов нет вовсе, расписания нет
    t = [n["text"] for n in gl.backup_notes(_inst(bk={"n": 0, "total": 0, "first": 0}, sched=[]), _rep(), NOW)]
    assert "в каталоге бэкапов нет ни одного архива gitlab-backup" in t
    assert "крон или таймер с gitlab-backup не найден" in t
    # ночной крон без репозиториев - подсветить
    sched = [{"how": "cron", "src": "/var/spool/cron/crontabs/root", "when": "0 1 * * *", "db": 1,
              "skip": "repositories,lfs,registry", "etc": 0}]
    last = {"name": "x", "ts": TS - 3600, "size": 1, "info": 1, "skipped": "lfs,registry,repositories", "ver": "19.4.1-ee"}
    t = [n["text"] for n in gl.backup_notes(_inst(sched=sched, bk={"n": 6, "total": 1, "first": 0, "last": last}), _rep(), NOW)]
    assert t == ["бэкап без LFS, репозиториев (SKIP=lfs,registry,repositories)"]
    # старый бэкап без расписания
    old = {**last, "ts": TS - 27 * DAY, "skipped": "artifacts,registry"}
    t = [n["text"] for n in gl.backup_notes(_inst(sched=[], bk={"n": 5, "total": 1, "first": 0, "last": old}), _rep(), NOW)]
    assert t == ["последний бэкап 27 дней назад, по расписанию не делается"]
    # конфиг: нет архива и нет restic-бэкапа Kervax - без gitlab-secrets.json базу не прочитать
    t = [n["text"] for n in gl.backup_notes(_inst(cfg=None), _rep(backup={}), NOW)]
    assert any("gitlab-secrets.json" in x for x in t)
    # конфиг покрыт restic-бэкапом Kervax - не беда, хоть архива и нет
    assert gl.backup_notes(_inst(cfg=None), _rep(), NOW) == []
    inc = {"configured": True, "mode": "include", "includes": ["/var/www"]}
    assert gl.restic_covers(_rep(backup=inc), "/app/gitlab/config") is False
    assert gl.restic_covers(_rep(), "/app/gitlab/config") is True
    # таймер бэкапа упал
    bad = [{**_inst()["sched"][0], "result": "exit-code"}]
    t = [n["text"] for n in gl.backup_notes(_inst(sched=bad), _rep(), NOW)]
    assert t == ["последний запуск gitlab-backup.timer завершился с ошибкой (exit-code)"]


def test_view(monkeypatch):
    monkeypatch.setattr(gl, "PATCH_DAYS", 7)
    v = gl.view(_srv(), NOW)
    assert v["patch_days"] == 7 and v["feed"]["stops_src"] == "upgrade_path.yml"
    row = v["instances"][0]
    assert row["label"] == "gitlab.example.com" and row["st"]["target"] == "19.5.1"
    assert row["restic_cfg"] is True and row["causes"] == ["patch"] and row["notes"] == []
    v = gl.view(_srv(mutes=["gitlab:gitlab.example.com"]), NOW)
    assert v["instances"][0]["mute"] == {"perm": True}
    assert gl.view(_srv(_rep(ts=TS - 7200)), NOW) is None


def test_setup_needed():
    ct = {"name": "gitlab-gitlab-ee-1", "image": "gitlab-gitlab-ee", "compose": {"service": "gitlab-ee"}}
    assert setup_scripts.setup_needed("gitlab-setup", {"docker": {"containers": [ct]}})
    runner = {"name": "gitlab", "image": "gitlab/gitlab-runner:v18.0", "compose": {"service": "gitlab"}}
    assert not setup_scripts.setup_needed("gitlab-setup", {"docker": {"containers": [runner]}})
    assert not setup_scripts.setup_needed("gitlab-setup", {"extras": {"gitlab": {"v": 1, "ts": TS, "instances": []}}})
    assert setup_scripts.setup_needed("gitlab-setup", _rep())



def test_when_finds_a_gitlab_service_on_an_image_of_its_own():
    """Панель зовет gitlab-setup и для compose-сервиса gitlab на своем образе. Условие из скрипта
    раньше искало в docker ps только gitlab-ce/ee: плейбук такую ноду пропускал, а панель держала
    ее в kervax_outdated. Теперь условие спрашивает и метку сервиса compose."""
    import re
    ct = {"name": "gl", "image": "registry.example/ops/omnibus:17", "compose": {"service": "gitlab"}}
    assert setup_scripts.setup_needed("gitlab-setup", {"docker": {"containers": [ct]}})
    src = (Path(__file__).resolve().parents[2] / "agent" / "gitlab-setup.sh").read_text(encoding="utf-8")
    when = re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', src, re.MULTILINE).group(1)
    assert "--filter label=com.docker.compose.service=gitlab " in when and "'" not in when


def test_helper_version_line_and_lf():
    p = Path(__file__).resolve().parents[2] / "agent" / "gitlab-setup.sh"
    raw = p.read_bytes()
    assert b"\r\n" not in raw
    line = next(x for x in raw.decode().splitlines() if x.startswith("KERVAX_SETUP_VERSION="))
    assert line.split("=", 1)[1].split()[0].count(".") == 1 and "  #" in line


async def test_restart_without_feed_in_memory_keeps_alert(tmp_path, monkeypatch):
    """Перезапуск планировщика: выпусков в памяти нет до стадии "выпуски GitLab". Открытая тревога не
    получает отбоя и через минуту ту же тревогу снова: выпуски берутся из базы, а без них уровень замирает"""
    import json

    from sqlalchemy import delete, select

    from app import settings_store
    from app.alerts import full_text
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import AppSetting

    monkeypatch.setattr(gl, "PATCH_DAYS", 7)
    monkeypatch.setattr(gl, "STOP_DAYS", 14)

    def factory():
        return create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")

    engine, fac = factory()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with fac() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report={"uptime_seconds": 100000}))
        await settings_store.set_raw(s, gl.FEED_KEY, json.dumps(FEED))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(minute, inst=None):
        t = NOW.replace(minute=minute)
        async with fac() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = t
            srv.last_report = {"uptime_seconds": 100000, **_rep([inst or _inst()], ts=int(t.timestamp())),
                               "clock_unix": t.timestamp()}
            await s.commit()
        await collector.evaluate_servers(fac, cfg, t)

    await tick(1)
    assert len(sent) == 1 and "19.4.1, патч 19.4.2" in full_text(sent[0])
    # перезапуск: новый процесс, в памяти выпусков нет, в базе есть
    await engine.dispose()
    gl.set_feed(None)
    engine, fac = factory()
    await tick(2)
    await tick(3)
    assert len(sent) == 1
    # выпусков нет и в базе (не прочитались): объявленное замирает, без отбоя
    async with fac() as s:
        await s.execute(delete(AppSetting).where(AppSetting.key == gl.FEED_KEY))
        await s.commit()
    gl.set_feed(None)
    await tick(4)
    assert len(sent) == 1
    async with fac() as s:
        assert (await s.execute(select(Server))).scalars().one().alert_state["gitlab"] == 1
    # а сломанное автообновление видно и без выпусков: это новость
    au = {"broken": "2026-10-22 23:50:00 19.4.1 -> 19.5.1: откат не удался", "blocked": ""}
    await tick(5, _inst(au=au))
    assert len(sent) == 2 and "сломан" in full_text(sent[1])
    # выпуски вернулись, GitLab обновили - честный отбой
    async with fac() as s:
        await settings_store.set_raw(s, gl.FEED_KEY, json.dumps(FEED))
        await s.commit()
    gl.set_feed(None)
    await tick(6, _inst(version="19.5.1"))
    assert len(sent) == 3 and "снова в порядке" in sent[2]
    await engine.dispose()
