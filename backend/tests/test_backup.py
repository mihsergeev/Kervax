import httpx


async def test_backup_export_restore_roundtrip(
    client: httpx.AsyncClient, auth_headers
):
    # создаём монитор и сервер
    r = await client.post(
        "/api/checks",
        json={"name": "m1", "type": "http", "target": "https://ex.com"},
        headers=auth_headers,
    )
    assert r.status_code in (200, 201)
    r = await client.post("/api/servers", json={"name": "s1"}, headers=auth_headers)
    assert r.status_code == 201

    # экспорт: есть монитор, метрик НЕТ
    r = await client.get("/api/backup/export", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["app"] == "kervax"
    assert any(c["name"] == "m1" for c in data["tables"]["checks"])
    assert any(s["name"] == "s1" for s in data["tables"]["servers"])
    assert "server_metrics" not in data["tables"]
    assert "check_samples" not in data["tables"]

    # удаляем все мониторы
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    for c in ov["checks"]:
        await client.delete(f"/api/checks/{c['id']}", headers=auth_headers)
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["total"] == 0

    # восстановление возвращает монитор
    r = await client.post("/api/backup/restore", json=data, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["restored"]["checks"] >= 1

    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert any(c["name"] == "m1" for c in ov["checks"])


async def test_backup_restore_rejects_foreign_file(
    client: httpx.AsyncClient, auth_headers
):
    r = await client.post(
        "/api/backup/restore", json={"foo": "bar"}, headers=auth_headers
    )
    assert r.status_code == 400


async def test_backup_config_roundtrip(client: httpx.AsyncClient, auth_headers):
    r = await client.put(
        "/api/backup/config",
        json={"interval_hours": 12, "keep": 30},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json() == {"interval_hours": 12, "keep": 30}
    r = await client.get("/api/backup/config", headers=auth_headers)
    assert r.json()["interval_hours"] == 12

def test_rotation_overflow_needs_time_and_zero_removals():
    """Мёртвую ротацию видно на вторые сутки: снапшотов больше политики, удалений нет.

    Возраст старейшего снапшота при keep-monthly 6 терпит 231 день — за это время диск
    активного бэкап-сервера кончится дважды. Но переполнение САМО ПО СЕБЕ законно:
    forget группирует по host+tags, и репозиторий с двумя клиентами держит два
    комплекта. Отличает мёртвую ротацию именно ноль удалений.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    bsrv = {"repos": [
        # ротация встала: снапшотов втрое больше политики и ничего не уходит
        {"name": "мертвый", "snapshots": 42, "keep_daily": 7, "keep_weekly": 4,
         "keep_monthly": 6, "rotation_removed": 0},
        # переполнен, но прогоны что-то удаляют — это просто несколько групп
        {"name": "многогруппный", "snapshots": 34, "keep_daily": 7, "keep_weekly": 4,
         "keep_monthly": 6, "rotation_removed": 5},
        # политики нет — судить не о чем
        {"name": "без-политики", "snapshots": 99, "rotation_removed": 0},
    ]}

    out, seen = collector.rotation_overflow(bsrv, {}, now)
    assert out == [], "сработал в первый же тик — единичный лок дал бы ложную тревогу"
    assert set(seen) == {"мертвый"}, seen

    old = {"мертвый": (now - timedelta(days=4)).isoformat()}
    out, seen = collector.rotation_overflow(bsrv, old, now)
    assert len(out) == 1 and out[0].startswith("мертвый")
    assert "42" in out[0] and "17" in out[0], "в тексте нет ни числа снапшотов, ни политики"

    # ротация ожила — отметка уходит, алерта нет
    bsrv["repos"][0]["rotation_removed"] = 11
    out, seen = collector.rotation_overflow(bsrv, old, now)
    assert out == [] and seen == {}


def test_lock_alert_waits_a_day_and_counts_from_the_oldest_stale_lock():
    """Висячий лок моложе суток снимут сами чистка и клиент (restic unlock перед работой).
    Старше суток - ежедневная чистка на нем уже споткнулась: на backup-a старый общий
    скрипт unlock не делал, и ротация app-a стояла 23 дня."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    bsrv = {"repos": [
        {"name": "idet-bekap", "valid": True, "locked": True, "lock_ts": ts - 120},
        {"name": "vchera-upal", "valid": True, "locked": True, "lock_ts": ts - 5 * 3600},
        {"name": "stoit", "valid": True, "locked": True, "lock_ts": ts - 3 * 86400 - 60},
        {"name": "zaglushen", "valid": True, "locked": True, "lock_ts": ts - 9 * 86400},
        {"name": "bez-vremeni", "valid": True, "locked": True, "lock_ts": 0},
    ]}
    assert collector.long_locked_repos(bsrv, {"zaglushen"}, now) == [("stoit", 3)]
    # лок без времени (очень старый helper) остается в общем алерте: его возраст не узнать
    assert collector._backup_problem_repos(bsrv, set(), now) == ["bez-vremeni"]
    assert collector._backup_repo_reason(bsrv["repos"][4], now) == "залочен"


def test_repo_alert_says_why():
    from datetime import datetime, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    assert collector._backup_repo_reason({"valid": False}, now) == "нет config"
    old = {"valid": True, "last_activity": now.timestamp() - 5 * 86400 - 60}
    assert collector._backup_repo_reason(old, now) == "нет новых бэкапов 5 дн"
    assert collector._backup_repo_reason({"valid": True, "last_activity": now.timestamp()}, now) == ""


def test_rotation_overflow_sees_removals_without_prune_metrics():
    """Репозиторий чистит старый общий скрипт: метрик удалений нет, раньше он не проверялся
    вовсе. Удаления видит helper по пропавшим файлам снапшотов."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    repo = {"name": "legacy", "snapshots": 30, "keep_daily": 7, "keep_weekly": 4,
            "keep_monthly": 6, "rotation_removed": -1, "last_activity": ts - 3600}
    bsrv = {"repos": [repo]}

    def extra(removed_ts, seen_since):
        return {"repos": {"legacy": {"cleaner": "legacy", "removed_ts": removed_ts,
                                     "seen_since": seen_since}}}

    # helper старый - судить не по чему
    assert collector.rotation_overflow(bsrv, {}, now)[0] == []
    # удаляли вчера: ротация живая, просто несколько групп
    assert collector.rotation_overflow(bsrv, {}, now, extra(ts - 86400, ts - 30 * 86400))[0] == []
    # смотрим неделю, удалений не было ни одного
    out, _ = collector.rotation_overflow(bsrv, {}, now, extra(0, ts - 7 * 86400))
    assert len(out) == 1 and "удалений нет 7 дн" in out[0] and "17" in out[0], out
    # бэкапы перестали приходить: удалять нечего, это уже алерт backup_repo
    repo["last_activity"] = ts - 4 * 86400
    assert collector.rotation_overflow(bsrv, {}, now, extra(0, ts - 7 * 86400))[0] == []


def test_backup_server_extra_must_be_fresh():
    from datetime import datetime, timezone

    from app import collector

    ts = datetime.now(timezone.utc).timestamp()
    block = {"v": 1, "ts": ts - 60, "repos": {"a": {"cleaner": "legacy"}}}
    rep = {"clock_unix": ts, "extras": {"backup-server": block}}
    assert collector.bsrv_extra(rep) == block
    rep["extras"]["backup-server"] = dict(block, ts=ts - 3600)
    assert collector.bsrv_extra(rep) == {}
    rep["extras"]["backup-server"] = dict(block, v=2)
    assert collector.bsrv_extra(rep) == {}


def test_rotation_age_trusts_the_policy_when_it_can_keep_everything():
    """keep-daily считает дни с бэкапами, а не календарные. app-d бэкапится раз в
    месяц-другой: 7 снапшотов за 15 месяцев, все семь законно держит --keep-daily 7, хотя
    старейшему 458 дней. А у vpn-c 28 снапшотов: текущая группа обрезается по
    политике, а старая (сменился хост или пути) застыла в 2024 году и не уйдет никогда."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    pol = {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
    sparse = dict(pol, name="app-d", snapshots=7, oldest_snapshot=ts - 458 * 86400)
    orphan = dict(pol, name="vpn-c", snapshots=28, oldest_snapshot=ts - 859 * 86400,
                  rotation_removed=-1)
    bsrv = {"repos": [sparse, orphan]}
    out = collector.rotation_stale_repos(bsrv, now)
    assert out == ["vpn-c (859 дн. > 231)"], out
    # чистка при этом идет: подсказываем, что это старая группа, а не вставшая ротация
    extra = {"repos": {"vpn-c": {"cleaner": "legacy", "removed_ts": ts - 3600,
                                         "seen_since": ts - 86400}}}
    out = collector.rotation_stale_repos(bsrv, now, extra)
    assert len(out) == 1 and "старую группу" in out[0], out


def test_backup_clients_without_an_agent_are_found_by_name():
    """Репозиторий назван по клиенту, и клиента ищем среди нод панели: по имени, hostname и
    имени машины из отчета агента. Устаревшие (клиента, похоже, уже нет) и заглушенные не в счет."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    nodes = [
        SimpleNamespace(name="stats-a", hostname="", enabled=True, last_report={}),
        SimpleNamespace(name="db", hostname="", enabled=True, last_report={"hostname": "db-dev1.example"}),
        SimpleNamespace(name="node-old", hostname="", enabled=False, last_report={}),
    ]
    names = collector.panel_server_names(nodes)
    bs = {"repos": [
        {"name": "stats-a", "last_activity": ts - 3600},
        {"name": "db-dev1", "last_activity": ts - 3600},     # по имени машины из отчета
        {"name": "app-a", "last_activity": ts - 3600},    # агента нет
        {"name": "node-old", "last_activity": ts - 3600},        # сервер в панели выключен
        {"name": "zaglushen", "last_activity": ts - 3600},
        {"name": "davno-net", "last_activity": ts - 9 * 86400},  # клиента уже нет
    ]}
    assert collector.backup_unmonitored(bs, {"zaglushen"}, names, now) == ["app-a", "node-old"]


async def test_new_backup_client_without_an_agent_alerts_once(tmp_path, monkeypatch):
    """Клиенты, которые уже писали на сервер, когда проверка появилась, запоминаются молча:
    иначе первое сообщение было бы на 60 строк. Алерт приходит про нового и один раз."""
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = now.timestamp()

    def report(*names):
        return {"cpu_percent": 5, "backup_server": {"present": True, "running": True, "repos": [
            {"name": n, "valid": True, "snapshots": 5, "last_activity": ts - 600} for n in names]}}

    async with factory() as s:
        s.add(Server(name="backup-01", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report=report("stats-a", "app-a"),
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0))
        s.add(Server(name="stats-a", token_hash="y", enabled=True, backup_not_required=True,
                     last_seen=now, last_report={"cpu_percent": 5},
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert not [x for x in sent if "без агента" in x]  # app-a был и раньше

    async with factory() as s:
        bsrv = (await s.scalars(collector.select(Server).where(Server.name == "backup-01"))).first()
        bsrv.last_report = report("stats-a", "app-a", "node-new")
        await s.commit()
    await collector.evaluate_servers(factory, settings, now + timedelta(seconds=30))
    hits = [x for x in sent if "без агента" in x]
    assert len(hits) == 1 and "node-new" in hits[0] and "app-a" not in hits[0], sent
    await collector.evaluate_servers(factory, settings, now + timedelta(seconds=60))
    assert len([x for x in sent if "без агента" in x]) == 1  # не повторяется
    await engine.dispose()


def test_home_page_rotation_matches_the_alert():
    """"Что сломано" на главной берет ротацию теми же правилами, что и алерт: старая группа
    vpn-c там видна, заглушенный репозиторий - нет."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    pol = {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
    s = SimpleNamespace(
        last_report={"backup_server": {"present": True, "repos": [
            dict(pol, name="vpn-c", snapshots=28, oldest_snapshot=ts - 859 * 86400),
            dict(pol, name="zaglushen", snapshots=28, oldest_snapshot=ts - 900 * 86400),
        ]}},
        alert_state={}, backup_repo_mutes=["zaglushen"],
    )
    assert collector.backup_rotation_items(s, now) == ["vpn-c (859 дн. > 231)"]


def test_failed_integrity_checks():
    """Недельная проверка нашего prune-скрипта: алертим, только если последняя проверка свежая и
    не прошла. Еще не было (-1) и прошедшие давно (скрипт, похоже, не запускается) не в счет."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    bs = {"repos": [{"name": n} for n in ("broken", "ok", "new", "old", "muted")]}
    extra = {"repos": {
        "broken": {"check_ts": ts - 3600, "check_ok": 0},
        "ok": {"check_ts": ts - 3600, "check_ok": 1},
        "new": {"check_ts": ts - 3600, "check_ok": -1},
        "old": {"check_ts": ts - 30 * 86400, "check_ok": 0},
        "muted": {"check_ts": ts - 3600, "check_ok": 0},
    }}
    assert collector.failed_checks(bs, extra, {"muted"}, now) == ["broken"]


def test_backup_server_old_restic():
    """restic самого сервера бэкапов сравниваем с целью из того же блока helper'а, по числам."""
    from datetime import datetime, timezone

    from app import collector

    ts = datetime.now(timezone.utc).timestamp()

    def rep(cur, want="0.19.1", age=60):
        return {"clock_unix": ts, "extras": {"backup-server": {
            "v": 1, "ts": ts - age, "restic": cur, "restic_target": want, "repos": {}}}}

    assert collector.bsrv_restic_old(rep("0.14.0")) == "0.14.0"
    assert collector.bsrv_restic_old(rep("0.9.6")) == "0.9.6"  # не строками: "0.9" > "0.1"
    assert collector.bsrv_restic_old(rep("0.19.1")) == ""
    assert collector.bsrv_restic_old(rep("0.20.0")) == ""
    assert collector.bsrv_restic_old(rep("")) == ""  # helper не нашел restic
    assert collector.bsrv_restic_old(rep("0.14.0", want="")) == ""
    assert collector.bsrv_restic_old(rep("0.14.0", age=3600)) == ""  # helper встал
    assert collector.bsrv_restic_old({}) == ""


async def test_old_restic_on_backup_server_goes_to_server_field_and_ansible(client, auth_headers):
    import time

    r = await client.post("/api/servers", json={"name": "bsrv"}, headers=auth_headers)
    token = r.json()["token"]
    now = int(time.time())
    rep = {"hostname": "bsrv", "os": "Debian", "agent_version": "2.23", "cpu_percent": 5,
           "mem_used": 1, "mem_total": 2, "clock_unix": now,
           "extras": {"backup-server": {"v": 1, "ts": now - 30, "restic": "0.14.0",
                                        "restic_target": "0.19.1", "legacy": None, "repos": {}}}}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    srv = [x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["name"] == "bsrv"][0]
    assert srv["bsrv_restic_old"] == "0.14.0"
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    atok = r.json()["token"]
    rows = {x["name"]: x for x in (await client.get(
        "/api/ansible/servers", headers={"Authorization": f"Bearer {atok}"})).json()["servers"]}
    assert rows["bsrv"]["issues"] == ["restic_old"]


def test_backup_cadence_from_the_head_of_the_snapshot_list():
    """Ритм - по самым свежим интервалам, пока они сходятся с самым новым: глубже снапшоты
    проредила политика хранения, и интервалы там длиннее настоящих."""
    from app import collector

    h = 3600
    t0 = 1_800_000_000
    daily = [t0 - i * 24 * h for i in range(8)]
    # как на серверах: после 7 суточных идут 72 и 168 часов
    assert collector.backup_cadence(daily + [daily[-1] - 72 * h, daily[-1] - 240 * h]) == 24 * h
    assert collector.backup_cadence([t0 - i * h for i in range(6)]) == h
    # снапшоты одного прогона (файлы и дампы отдельно) - один бэкап
    assert collector.backup_cadence([t0, t0 - 60, t0 - 24 * h, t0 - 24 * h - 90, t0 - 48 * h]) == 24 * h
    # keep-daily 2: один суточный интервал, дальше недельные - ритм не ясен, а не "раз в неделю"
    assert collector.backup_cadence([t0, t0 - 24 * h, t0 - 192 * h, t0 - 360 * h]) == 0
    # ручной бэкап посреди дня: пока он в голове, ритм не ясен (лучше 3 дня, чем ложный алерт)
    assert collector.backup_cadence([t0, t0 - 2 * h, t0 - 24 * h, t0 - 48 * h]) == 0
    # app-d: редкие и неровные бэкапы
    assert collector.backup_cadence([t0, t0 - 153 * h, t0 - 6549 * h, t0 - 8066 * h]) == 0
    assert collector.backup_cadence([t0]) == 0 and collector.backup_cadence(None) == 0


def test_late_backup_of_a_client_without_an_agent():
    """С известным ритмом клиент без агента опаздывает через интервал и четверть его; у клиента
    с агентом порог не короче прежних 3 дней, а у недельного бэкапа он шире."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    ts = now.timestamp()
    h = 3600

    def repo(age_h):
        return {"name": "web-01", "valid": True, "last_activity": ts - age_h * h}

    daily, weekly = 24 * h, 168 * h
    assert collector._backup_repo_reason(repo(29), now, daily, monitored=False) == ""
    assert collector._backup_repo_reason(repo(31), now, daily, monitored=False) == \
        "бэкап опоздал: обычно раз в сутки, последний 31 ч назад"
    assert collector._backup_repo_reason(repo(31), now, daily, monitored=True) == ""
    assert collector._backup_repo_reason(repo(80), now, daily, monitored=True).startswith("бэкап опоздал")
    # недельный: 4 дня - не устарел (раньше был алерт через 3 дня), 9 дней - опоздал
    assert collector._backup_repo_reason(repo(96), now, weekly, monitored=True) == ""
    assert collector._backup_repo_reason(repo(216), now, weekly, monitored=False) == \
        "бэкап опоздал: обычно раз в неделю, последний 9 дн назад"
    # ритм не ясен - как раньше
    assert collector._backup_repo_reason(repo(96), now, 0, monitored=False) == "нет новых бэкапов 4 дн"
    # ритм из блока helper'а и клиент по именам нод панели
    extra = {"repos": {"web-01": {"recent": [ts - 31 * h - i * daily for i in range(5)]}}}
    assert collector.repo_reason(repo(31), now, extra, {"backup-01"}).startswith("бэкап опоздал")
    assert collector.repo_reason(repo(31), now, extra, {"web-01"}) == ""


def test_archive_is_not_a_problem_and_old_groups_are_named():
    """Архив не устаревает, не ротация и не "клиент без агента"; старая группа называется по
    хосту, а убранная в архив больше не делает ротацию встающей."""
    from datetime import datetime, timezone

    from app import collector

    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    ts = now.timestamp()
    d = 86400
    pol = {"keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
    gone = {"name": "vpn-b", "valid": True, "snapshots": 14, "last_activity": ts - 700 * d,
            "oldest_snapshot": ts - 900 * d, **pol}
    renamed = {"name": "vpn-a", "valid": True, "snapshots": 29, "last_activity": ts - 3600,
               "oldest_snapshot": ts - 860 * d, "rotation_removed": 1, **pol}
    bs = {"repos": [gone, renamed]}
    extra = {"repos": {"vpn-a": {"groups": {"ts": ts - 3600, "groups": [
        {"host": "server", "n": 17, "first": ts - 200 * d, "last": ts - 3600},
        {"host": "vpn-a", "n": 12, "first": ts - 860 * d, "last": ts - 856 * d},
    ]}}}}
    assert collector._backup_problem_repos(bs, set(), now) == ["vpn-b"]
    assert collector._backup_problem_repos(bs, set(), now, archive={"vpn-b": {}}) == []
    assert collector.backup_unmonitored({"repos": [renamed]}, set(), set(), now) == ["vpn-a"]
    assert collector.backup_unmonitored({"repos": [renamed]}, set(), set(), now, {"vpn-a": {}}) == []
    stale = collector.rotation_stale_repos(bs, now, extra)
    assert stale == ["vpn-a (860 дн. > 231, старая группа vpn-a: 12 снапшотов)"], stale
    assert collector.rotation_stale_repos(bs, now, extra, {"vpn-a|vpn-a": {}}) == []
    assert collector.rotation_stale_repos(bs, now, extra, {"vpn-a": {}}) == []
    assert [g["host"] for g in collector.old_groups(extra, None, "vpn-a", ts)] == ["vpn-a"]
    # группы старше трех суток (prune-скрипт не запускается) - не судим по ним
    stale_groups = {"repos": {"vpn-a": {"groups": dict(extra["repos"]["vpn-a"]["groups"], ts=ts - 5 * d)}}}
    assert collector.old_groups(stale_groups, None, "vpn-a", ts) == []


async def test_archive_endpoint_and_late_reasons_in_the_server_list(client, auth_headers):
    import time

    r = await client.post("/api/servers", json={"name": "backup-02"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    now = int(time.time())
    h = 3600
    rep = {"hostname": "backup-02", "os": "Debian", "agent_version": "2.23", "cpu_percent": 5,
           "mem_used": 1, "mem_total": 2, "clock_unix": now,
           "backup_server": {"present": True, "running": True, "repos": [
               {"name": "gone-client", "valid": True, "snapshots": 9, "size_bytes": 1,
                "last_activity": now - 31 * h},
               {"name": "vpn-b", "valid": True, "snapshots": 14, "size_bytes": 1,
                "last_activity": now - 700 * 86400}]},
           "extras": {"backup-server": {"v": 1, "ts": now - 30, "legacy": None, "repos": {
               "gone-client": {"recent": [now - 31 * h - i * 24 * h for i in range(5)]}}}}}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    def mine(rows):
        return [x for x in rows if x["id"] == sid][0]

    srv = mine((await client.get("/api/servers", headers=auth_headers)).json())
    assert srv["bsrv_cadence"] == {"gone-client": 24 * h}
    assert srv["bsrv_repo_reasons"] == {
        "gone-client": "бэкап опоздал: обычно раз в сутки, последний 31 ч назад",
        "vpn-b": "нет новых бэкапов 700 дн"}
    r = await client.post(f"/api/servers/{sid}/backup/repo-archive", headers=auth_headers,
                          json={"repo": "vpn-b", "note": "сервера больше нет", "archived": True})
    assert r.status_code == 200, r.text
    assert r.json()["backup_repo_archive"]["vpn-b"]["note"] == "сервера больше нет"
    srv = mine((await client.get("/api/servers", headers=auth_headers)).json())
    assert list(srv["bsrv_repo_reasons"]) == ["gone-client"]
    r = await client.post(f"/api/servers/{sid}/backup/repo-archive", headers=auth_headers,
                          json={"repo": "vpn-b", "host": "old host", "archived": True})
    assert r.status_code == 422  # хост только из безопасных символов
    r = await client.post(f"/api/servers/{sid}/backup/repo-archive", headers=auth_headers,
                          json={"repo": "vpn-b", "archived": False})
    assert r.json()["backup_repo_archive"] == {}


async def test_new_late_repo_alerts_at_once_next_to_an_old_problem(tmp_path, monkeypatch):
    """Опоздавший бэкап клиента без агента приходит сразу, а не с суточным напоминанием про
    репозиторий, который уже был проблемным."""
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'l.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    h = 3600

    def report(late_age_h):
        last = ts - late_age_h * h
        return {"cpu_percent": 5, "clock_unix": ts,
                "backup_server": {"present": True, "running": True, "repos": [
                    {"name": "vpn-b", "valid": True, "snapshots": 14, "last_activity": ts - 700 * 86400},
                    {"name": "web-07", "valid": True, "snapshots": 9, "last_activity": last}]},
                "extras": {"backup-server": {"v": 1, "ts": ts - 30, "repos": {
                    "web-07": {"recent": [last - i * 24 * h for i in range(5)]}}}}}

    async with factory() as s:
        s.add(Server(name="backup-03", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report=report(5),
                     cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    first = [x for x in sent if "vpn-b" in x]
    assert len(first) == 1 and "web-07" not in first[0], sent
    async with factory() as s:
        bsrv = (await s.scalars(collector.select(Server).where(Server.name == "backup-03"))).first()
        bsrv.last_report = report(31)
        bsrv.last_seen = now + timedelta(seconds=30)
        await s.commit()
    await collector.evaluate_servers(factory, settings, now + timedelta(seconds=30))
    late = [x for x in sent if "web-07" in x]
    assert len(late) == 1 and "бэкап опоздал" in late[0], sent
    await engine.dispose()
