"""Короткий алерт в Telegram (формат A, app/tg_fmt.py) у сервисов: воркеры, кроны, ошибки в логах,
очереди, PHP-FPM, здоровье баз, свои бэкапы, проверка восстановления. Вход - то, что модуль отдает
collector'у (список из problems()), выход - (что случилось, конкретика)."""
from datetime import datetime, timezone

from app import (backup_verify, cron_watch, custom_backups, db_health, fpm_pools, log_errors,
                 queue_watch, workers)
from app.models import Server

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
BAD = set("—–≥≤↳«»“”…·→ёЁ•")


def _srv(rep, state=None):
    return Server(id=1, name="app-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _ok(out):
    """Формат A: what - одна фраза с заглавной, конкретики 0-2 строки, только символы с клавиатуры"""
    what, facts = out
    assert what and what[0].isupper() and "\n" not in what
    assert len(facts) <= 2
    for line in [what, *facts]:
        assert not BAD & set(line), line
        assert len(line) <= 75, line
    return out


# ---- воркеры ----

def _workers_rep(progs, where="container worker-a", extra=None):
    ds = [{"kind": "supervisor", "where": where, "progs": progs}] + (extra or [])
    return {"clock_unix": TS, "extras": {"workers": {"v": 1, "ts": TS, "daemons": ds}}}


def _copies(n, bad, prog="queue-listen"):
    return [{"name": f"{prog}:{prog}_{i:02d}", "state": "RUNNING", "since": TS - 60, "r15": 4 if i < bad else 0}
            for i in range(n)]


def test_workers_copies_falling():
    wp = workers.problems(_srv(_workers_rep(_copies(15, 11))), NOW)
    assert _ok(workers.tg_short(wp)) == ("Воркеры падают и поднимаются",
                                         ["queue-listen x11 в worker-a"])


def test_workers_worst_first_rest_by_name():
    progs = _copies(15, 3) + [{"name": "mailer", "state": "FATAL", "since": TS - 600},
                              {"name": "import", "state": "EXITED", "exit": 3, "since": TS - 600}]
    pm2 = {"kind": "pm2", "where": "pod shop/api-6b8b56c769-f4k8m", "progs": [{"name": "api", "state": "online", "r15": 5}]}
    wp = workers.problems(_srv(_workers_rep(progs, extra=[pm2])), NOW)
    what, facts = _ok(workers.tg_short(wp))
    assert what == "Воркер не запускается"
    assert facts == ["mailer в worker-a", "Еще: api, import, queue-listen"]
    # под - по ворклоаду, без хеша; код выхода у завершившегося
    loop = [x for x in wp if x["name"] == "api"]
    assert workers.tg_short(loop) == ("Воркер падает и поднимается", ["api в shop/api"])
    ex = [x for x in wp if x["name"] == "import"]
    assert workers.tg_short(ex) == ("Воркер завершился с ошибкой", ["import в worker-a, код 3"])


def test_workers_empty():
    assert workers.tg_short([]) is None


# ---- кроны ----

def test_cron_cmd_and_schedule_in_words():
    assert cron_watch.cmd_short("/opt/app/bin/report-sync refresh >/dev/null 2>&1") == "report-sync refresh"
    assert cron_watch.cmd_short("cd /var/www/app && php artisan schedule:run >> /dev/null 2>&1") == "artisan schedule:run"
    assert cron_watch.cmd_short("/usr/bin/flock -n /tmp/s.lock /opt/app/scripts/sync.sh > /var/log/s.log 2>&1") == "sync.sh"
    assert cron_watch.cmd_short("timeout -s KILL 1h nice -n 19 /usr/bin/python3 -m celery -A proj beat") == "celery -A proj beat"
    assert cron_watch.cmd_short("sh -c 'cd /app && ./bin/console messenger:consume async --time-limit=3600'") == \
        "console messenger:consume async"
    assert cron_watch.cmd_short("test -x /usr/sbin/anacron || ( cd / && run-parts --report /etc/cron.daily )") == \
        "run-parts --report /etc/cron.daily"
    assert cron_watch.sched_words("* * * * *") == "раз в минуту"
    assert cron_watch.sched_words("*/5 * * * *") == "раз в 5 минут"
    assert cron_watch.sched_words("*/2 * * * *") == "раз в 2 минуты"
    assert cron_watch.sched_words("15 * * * *") == "раз в час"
    assert cron_watch.sched_words("0 */6 * * *") == "раз в 6 часов"
    assert cron_watch.sched_words("30 3 * * *") == "каждый день в 03:30"
    assert cron_watch.sched_words("@daily") == "раз в день"
    assert cron_watch.sched_words("0 3 * * 0") == "0 3 * * 0"


def _cron_rep(*jobs):
    return {"clock_unix": TS, "uptime_seconds": 10 ** 7, "extras": {"cron": {"v": 1, "ts": TS, "jobs": list(jobs)}}}


HUNG = {"where": "host", "cmd": "/opt/app/bin/report-sync refresh >/dev/null 2>&1", "sched": "* * * * *",
        "n": 1, "done": 5, "max": 0, "run": [{"age": 4000, "cpu": 1}]}
PILE = {"where": "container app-cron", "cmd": "/app/sync.sh", "sched": "*/5 * * * *", "n": 6, "done": 5, "max": 0,
        "run": [{"age": 3000, "cpu": 1}, {"age": 2000, "cpu": 1}]}


def test_cron_hung_and_pile():
    cp = cron_watch.problems(_srv(_cron_rep(HUNG)), NOW)
    assert _ok(cron_watch.tg_short(cp)) == ("Крон висит 1 ч", ["report-sync refresh, раз в минуту"])
    cp = cron_watch.problems(_srv(_cron_rep(HUNG, PILE)), NOW)
    assert _ok(cron_watch.tg_short(cp)) == ("Копии крона наслаиваются",
                                            ["sync.sh x6 в app-cron, раз в 5 минут", "Еще: report-sync refresh"])


def test_cron_empty():
    assert cron_watch.tg_short([]) is None


# ---- ошибки в логах ----

def _log_rep(*sources):
    return {"clock_unix": TS, "extras": {"logerr": {"v": 1, "ts": TS, "sources": list(sources)}}}


PROXY = {"where": "container proxy-caddy-1", "container": "", "h1": 643, "d1": 643 + 67 * 23, "span": 86400,
         "hb": [110] * 6, "last": '{"level":"error","msg":"dial tcp 172.18.0.14:8080: i/o timeout"}'}


def _pod(name, h1=3303, base=181, c="php"):
    return {"where": f"pod shop/{name}", "container": c, "h1": h1, "d1": h1 + base * 23, "span": 86400, "hb": [600] * 6}


def test_log_errors_container_and_pods():
    held = {"log_errors_sig": ["контейнер proxy-caddy-1|", "под shop/web|php"]}
    lp = log_errors.problems(_srv(_log_rep(PROXY), held), NOW)
    assert _ok(log_errors.tg_short(lp)) == ("Всплеск ошибок в логе",
                                            ["Контейнер: proxy-caddy-1, в 10 раз больше обычного"])
    # копии ворклоада - один лог, под по ворклоаду с контейнером
    lp = log_errors.problems(_srv(_log_rep(PROXY, _pod("web-6b8b56c769-f4k8m"), _pod("web-6b8b56c769-zzxxq")), held), NOW)
    assert [x["key"] for x in lp] == ["контейнер proxy-caddy-1|", "под shop/web|php"]
    assert _ok(log_errors.tg_short(lp[1:])) == ("Всплеск ошибок в логе", ["Под: shop/web (php), в 18 раз больше обычного"])
    assert log_errors.tg_short(lp)[1][1] == "Еще: shop/web"
    assert log_errors.tg_short(lp)[0] == "Всплеск ошибок в логах"


def test_log_errors_no_usual_and_empty():
    assert log_errors.tg_short([{"where": "контейнер app", "container": "", "h1": 30000, "base": None}]) == \
        ("Всплеск ошибок в логе", ["Контейнер: app"])
    assert log_errors.tg_short([{"where": "контейнер app", "container": "", "h1": 300, "base": 0.2}]) == \
        ("Всплеск ошибок в логе", ["Контейнер: app, обычно ошибок почти нет"])
    assert log_errors.tg_short([]) is None


# ---- очереди ----

def test_queue_stuck():
    mail = {"name": "mail", "source": "контейнер app-rabbitmq-1", "age": 4140, "d0": 25209, "depth": 25263,
             "consumers": 0, "stall": False}
    assert _ok(queue_watch.tg_short([mail])) == ("Очередь mail не разбирается",
                                                  ["нет потребителей 1 ч, 25 тыс. в app-rabbitmq-1"])
    tg = {"name": "notify", "source": "контейнер web-a-rabbitmq-1", "age": 14000, "depth": 160, "consumers": 2}
    crawl = {"name": "results", "source": "под default/rabbitmq-0", "age": 4000, "depth": 145, "consumers": 0}
    celery = {"name": "tasks", "source": "контейнер redis, Celery myns, db 1", "age": 7200, "depth": 300, "wait": 7200,
              "stall": True}
    assert _ok(queue_watch.tg_short([tg, crawl, celery])) == (
        "Очередь notify не разбирается",
        ["растет 3 ч, 160 в web-a-rabbitmq-1", "Еще: results, tasks"])
    assert _ok(queue_watch.tg_short([tg])) == ("Очередь notify не разбирается",
                                               ["растет 3 ч, 160 в web-a-rabbitmq-1", "потребителей 2"])
    assert queue_watch.tg_short([crawl]) == ("Очередь results не разбирается",
                                             ["нет потребителей 1 ч, 145 в rabbitmq", "+2 в мин"])
    # растет при живых потребителях: скорость, обычный уровень, сколько потребителей
    feed = {"name": "feed", "source": "контейнер rabbitmq", "age": 2400, "d0": 1000, "depth": 6000,
            "consumers": 15, "why": "grow", "base": 1200}
    assert _ok(queue_watch.tg_short([feed])) == (
        "Очередь feed не разбирается",
        ["растет 40 мин, 6000 в rabbitmq", "+125 в мин, обычно до 1200, потребителей 15"])
    slow = {"name": "rare", "source": "контейнер rabbitmq", "age": 3 * 3600, "d0": 500, "depth": 509,
            "consumers": 2, "why": "slow", "base": None}
    assert _ok(queue_watch.tg_short([slow])) == ("Очередь rare не разбирается",
                                                 ["3 ч не отдает ни одного, 509 в rabbitmq", "потребителей 2"])
    assert queue_watch.tg_short([celery]) == ("Очередь tasks стоит", ["задача ждет 2 ч, 300 в redis (Celery)"])
    assert queue_watch.tg_short([]) is None


def test_queue_stuck_from_problems():
    # потребителей нет 40 минут, условие держится 10: поле consumers и источник доходят до короткого текста
    src = "контейнер app-rabbitmq-1"
    st = {"qwatch": {f"{src}|//mail": {"t0": TS - 2400, "d0": 0, "p": 400, "nd": True, "last": 400, "seen": TS,
                                       "c": 0, "z0": TS - 2400, "zd": 0, "bad": TS - 600}}}
    rep = {"clock_unix": TS, "services": [{"kind": "rabbitmq", "source": src, "queues": [
        {"name": "mail", "vhost": "/", "ready": 400, "unacked": 0, "consumers": 0}]}]}
    qs = queue_watch.problems(_srv(rep, st), NOW)
    assert queue_watch.tg_short(qs) == ("Очередь mail не разбирается",
                                        ["нет потребителей 40 мин, 400 в app-rabbitmq-1", "+10 в мин"])
    # условие выполняется меньше 10 минут - еще не алерт
    st["qwatch"][f"{src}|//mail"]["bad"] = TS - 300
    assert queue_watch.problems(_srv(rep, st), NOW) == []


def test_queue_failed_and_depth():
    lara = {"source": "контейнер shop-php, Laravel failed_jobs, база app", "h1": 412, "base": 4.1}
    assert _ok(queue_watch.tg_short_failed([lara])) == (
        "Падают задачи", ["failed_jobs (Laravel) в shop-php, в 100 раз больше обычного"])
    celery = {"source": "на хосте, Celery, db 2", "h1": 600, "base": None}
    sk = {"source": "контейнер worker, Sidekiq", "h1": 300, "base": 0.2}
    assert _ok(queue_watch.tg_short_failed([celery, sk])) == ("Падают задачи", ["Celery на хосте", "Еще: Sidekiq в worker"])
    assert queue_watch.tg_short_failed([sk]) == ("Падают задачи", ["Sidekiq в worker, обычно задачи не падают"])
    assert queue_watch.tg_short_failed([]) is None
    assert _ok(queue_watch.tg_short_depth("emails", "контейнер app-rabbitmq-1", 15230)) == (
        "Выросла очередь emails", ["15 тыс. в app-rabbitmq-1"])


# ---- PHP-FPM ----

def _fpm_rep(pools, kern=None):
    b = {"v": 1, "ts": TS, "pools": pools}
    if kern:
        b["kern"] = kern
    return {"clock_unix": TS, "extras": {"fpm": b}}


MEDIA = {"where": "host", "pool": "media", "max": 50, "sat": 50, "reached": 80, "crash": 6,
       "crash_last": "WARNING: [pool media] child 123 exited on signal 11 (SIGSEGV) after 2.3 seconds"}
WWW = {"where": "container shop-php", "pool": "www", "max": 5, "tout": 30}


def test_fpm_busy_crash_timeout():
    fp = fpm_pools.problems(_srv(_fpm_rep([MEDIA, WWW])), NOW)
    assert _ok(fpm_pools.tg_short(fp)) == ("PHP-FPM не справляется",
                                           ["Пул: media, заняты все 50 воркеров", "воркеры еще и падают (SIGSEGV)"])
    assert _ok(fpm_pools.tg_short(fp[1:])) == ("PHP-FPM обрывает запросы по таймауту", ["Пул: www в shop-php"])
    # падения - проблема (от 10 за час), пул с очередью - предупреждение: первым худший
    crash = {"where": "container shop-php", "pool": "www", "max": 5, "crash": 12, "crash_last": "child 9 exited on signal 6"}
    fp = fpm_pools.problems(_srv(_fpm_rep([crash, {**WWW, "pool": "api", "sat": 20}])), NOW)
    assert _ok(fpm_pools.tg_short(fp)) == ("Падают воркеры PHP-FPM", ["Пул: www в shop-php, SIGABRT", "Еще: api в shop-php"])


def test_fpm_kernel_and_empty():
    kern = {"n": 12, "n_d": 30, "last": "php-fpm7.4[3321]: segfault at 0 ip 00007f sp 00007ff error 4 in libc.so.6"}
    fp = fpm_pools.problems(_srv(_fpm_rep([], kern)), NOW)
    assert _ok(fpm_pools.tg_short(fp)) == ("Падает PHP на ноде", ["php-fpm7.4, segfault"])
    assert fpm_pools.tg_short([]) is None


# ---- здоровье баз ----

def test_db_health_real_texts():
    rep = {"extras": {"db-health": {"v": 1, "ts": TS, "items": [
        {"engine": "pg", "container": "app-postgres-1", "idle_xmin": 8 * 3600 + 25 * 60, "idle_xmin_db": "app",
         "idle_xmin_app": "worker-a"},
        {"engine": "pg", "container": "postgresql", "xact": 3840, "xact_db": "shop"}]}}}
    dh = db_health.problems(rep, NOW)
    idle = [x for x in dh if x[2].endswith(":idle")]
    xact = [x for x in dh if x[2].endswith(":xact")]
    assert _ok(db_health.tg_short(idle)) == ("Транзакция висит 8 ч", ["база app, клиент worker-a"])
    assert _ok(db_health.tg_short(xact)) == ("Транзакция идет 1 ч", ["база shop"])
    assert _ok(db_health.tg_short(dh)) == ("Транзакция висит 8 ч", ["база app, клиент worker-a",
                                                                  "Еще: долгая транзакция"])


def test_db_health_kinds():
    rep = {"extras": {"db-health": {"v": 1, "ts": TS, "items": [
        {"engine": "pg", "container": "pg-replica", "role": "replica", "receiver": "streaming", "lag": 2000,
         "lock_waits": 3, "lock_wait": 600,
         "dead": [{"db": "app", "table": "events", "vac": -1, "dead": 3_400_000, "live": 10_000_000}]},
        {"engine": "mysql", "container": "", "role": "replica", "io": "No", "sql": "Yes", "cfg": {"version": "10.11.6-MariaDB"},
         "idle_xact": 9 * 3600, "idle_sleep": 8 * 3600, "idle_db": "shop", "idle_app": "php"},
        {"engine": "redis", "container": "app-redis", "rdb": "err", "stop_writes": "yes"},
        {"engine": "clickhouse", "container": "ch", "mut": {"n": 1, "tbl": "db.events", "age": 7 * 3600}}]}}}
    by = {k: (lv, t, k) for lv, t, k in db_health.problems(rep, NOW)}
    short = {k: _ok(db_health.tg_short([v])) for k, v in by.items()}
    assert short["pg:pg-replica:lag"] == ("Реплика PostgreSQL отстает на 33 мин", ["pg-replica"])
    assert short["pg:pg-replica:lock"] == ("Запросы ждут блокировку 10 мин", ["PostgreSQL pg-replica"])
    assert short["pg:pg-replica:vac:app.events"] == ("VACUUM ни разу не чистил таблицу", ["app.events, 3.4 млн мертвых строк"])
    assert short["mysql::stream"] == ("Репликация MariaDB стоит", ["на хосте"])
    assert short["mysql::idle"] == ("Транзакция висит 8 ч", ["база shop, клиент php"])
    assert short["redis:app-redis:rdb"] == ("Redis отклоняет запись", ["app-redis, не сохраняется снимок на диск"])
    assert short["clickhouse:ch:mut"] == ("Мутация ClickHouse идет 7 ч", ["таблица db.events"])
    # худшая первой, остальные названиями, не больше двух и "и еще"
    what, facts = db_health.tg_short(db_health.problems(rep, NOW))
    assert facts[1].startswith("Еще: ") and facts[1].endswith(" и еще 4")


def test_db_health_unknown_kind_and_empty():
    assert db_health.tg_short([(1, "PostgreSQL pg: что-то новое: подробности", "pg:pg:new")]) == ("Что-то новое", ["PostgreSQL pg"])
    assert db_health.tg_short([]) is None


# ---- свои бэкапы ----

def test_custom_backup():
    assert _ok(custom_backups.tg_short(["files-sync (последний прогон завершился с ошибкой)"])) == ("Упал свой бэкап files-sync", [])
    assert _ok(custom_backups.tg_short(["pg-dump (последний прогон не уложился в таймаут: app, billing)"])) == (
        "Упал свой бэкап pg-dump", ["не уложился в таймаут", "Базы: app, billing"])
    assert _ok(custom_backups.tg_short(["files-sync (нет свежего бэкапа 2.5 дн)", "graph-dump (таймер выключен — бэкап не запускается)",
                                        "dump.sh (cron не запускал задание 30 ч)"])) == (
        "Свой бэкап files-sync давно не отрабатывал", ["последний успешный 2 дня назад", "Еще: graph-dump, dump.sh"])
    assert custom_backups.tg_short(["graph-dump (таймер выключен — бэкап не запускается)"]) == (
        "Свой бэкап graph-dump не запускается", ["таймер выключен"])
    assert custom_backups.tg_short(["graph-dump (таймер выключен, последний прогон 3 дн назад)"]) == (
        "Свой бэкап graph-dump не запускается", ["таймер выключен, последний прогон 3 дня назад"])
    assert custom_backups.tg_short(["dump.sh (cron не запускал задание 30 ч)"]) == (
        "Cron не запускает свой бэкап dump.sh", ["последний запуск 30 ч назад"])
    assert custom_backups.tg_short([]) is None


# ---- проверка восстановления ----

def test_backup_restore():
    err = "snapshots: runuser: failed to execute /usr/local/bin/restic: No such file or directory"
    rep = {"extras": {"backup-verify": {"v": 1, "ts": TS, "err": err}}}
    assert _ok(backup_verify.tg_short(backup_verify.problems(rep, NOW))) == (
        "Не прошла проверка восстановления", ["нет бинарника restic"])
    rep = {"extras": {"backup-verify": {"v": 1, "ts": TS - 12 * 86400,
                                        "files": {"bad": ["/etc/nginx/nginx.conf", "/etc/passwd", "/var/www/a.php", "/x", "/y"]},
                                        "dumps": [{"engine": "mysql", "ok": False, "file": "mysql/all.sql.gz"}]}}}
    assert _ok(backup_verify.tg_short(backup_verify.problems(rep, NOW))) == (
        "Бэкап не восстанавливается", ["файлы не совпали с диском: nginx.conf, passwd, a.php и еще 2",
                                       "Еще: дамп mysql не читается, давно не проверялось"])
    assert backup_verify.tg_short([(1, "проверка восстановления не выполнилась: Fatal: wrong password or no key found")]) == (
        "Не прошла проверка восстановления", ["не подходит пароль репозитория"])
    assert backup_verify.tg_short([(1, "восстановление не проверялось 12 дней")]) == ("Восстановление не проверялось 12 дней", [])
    assert backup_verify.tg_short([]) is None
