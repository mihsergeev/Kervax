"""Короткие тексты Telegram (формат A, app/tg_fmt.py): OOM, рост памяти, частые перезапуски, GPU и
LLM, сертификаты и конфиги веб-серверов, прогноз диска, прирост бэкапа. На входе - то, что модули
уже отдают collector'у."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app import backup_growth, disk_forecast, gpu, mem_trend, oom, restarts, web_certs, web_config
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TS = NOW.timestamp()
MB = 2**20
POD_UID = "e551fc7f-1111-2222-3333-444455556666"
POD_CG = ("/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod"
          + POD_UID.replace("-", "_") + ".slice")
BAD = "—–≥↳«»·…ёЁ"


def _clean(what, facts):
    assert not any(c in what + "".join(facts) for c in BAD), (what, facts)
    return what, facts


def _srv(**kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report={}, alert_state={},
                alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0,
                disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0,
                disk_temp_alert_c=0, db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


# -- OOM --

def _kill(name, kind="global", memcg="", anon_mb=0, uid=0, user="root", oom_memcg="", limit_kb=None, dt=300):
    return {"ts": TS - dt, "pid": 100 + len(name), "name": name, "kind": kind, "memcg": memcg,
            "oom_memcg": oom_memcg, "uid": uid, "user": user, "anon": anon_mb * 1024,
            "mem": {"limit": limit_kb} if limit_kb else {}, "top": []}


def _block(kills, units=()):
    return {"v": 1, "ts": TS - 20, "kills": list(kills), "units": list(units)}


def _oom_rep(b):
    return {"clock_unix": TS, "extras": {"oom": b}}


POD_BLOCK = _block([
    _kill("mariadbd", "memcg", POD_CG + "/cri-containerd-ab.scope", 1012, 999, "", POD_CG, 1024 * 1024),
    _kill("healthcheck.sh", "memcg", "", 0, 0, "root", POD_CG, 1024 * 1024, dt=299),
])
HOST_BLOCK = _block(
    [_kill("php-fpm7.4", memcg="/system.slice/php7.4-fpm.service", anon_mb=391, uid=1001, user="idv"),
     _kill("systemd", memcg="/user.slice/user-1001.slice/user@1001.service/init.scope", anon_mb=2, uid=1001,
           user="idv", dt=299)],
    [{"ts": TS - 290, "unit": "mysql.service", "what": "failed"},
     {"ts": TS - 290, "unit": "php7.4-fpm.service", "what": "failed"},
     {"ts": TS - 290, "unit": "user@1001.service", "what": "oom"}])


def test_oom_in_pod_limit_names_the_pod():
    """Пример пользователя: убит mariadbd в лимите пода, под - по имени деплоймента, а не по uid."""
    e = oom.episodes(oom.records(POD_BLOCK))[-1]
    names = {"e551fc7f": "default/wordpress-mariadb-7d9f8b6c5d-x2k4q"}
    assert _clean(*oom.episode_short(e, timezone.utc, set(), {}, names)) == (
        "Кончилась память (OOM)", ["Убит: mariadbd", "Под: wordpress-mariadb (лимит 1гб)"])
    # имени нет (воркер без kube-api) - начало uid, как раньше
    assert oom.episode_short(e, timezone.utc, set())[1][1] == "Под: e551fc7f (лимит 1гб)"
    # через alert_step: короткий текст в "oom_short", сам алерт не изменился
    rep = _oom_rep(POD_BLOCK)
    s = SimpleNamespace(oom_total=2, oom_victim="", last_report=rep, alert_mutes=[])
    fire, done, _ = oom.alert_step(s, {"oom_end": 0.0}, rep, NOW, timezone.utc, set(), pod_names=names)
    assert len(fire) == 2 and fire[1].startswith("кончилась память в лимите под e551fc7f")
    assert done["oom_short"] == ("Кончилась память (OOM)", ["Убит: mariadbd", "Под: wordpress-mariadb (лимит 1гб)"])
    assert oom.tg_short(done["oom_short"]) == done["oom_short"] and oom.tg_short(None) is None


def test_oom_on_host_pool_and_restarted_units():
    e = oom.episodes(oom.records(HOST_BLOCK))[-1]
    # без "больше всего памяти", RAM/swap и счетчика за неделю; сессия пользователя - мелочь
    assert _clean(*oom.episode_short(e, timezone.utc, set())) == (
        "Кончилась память (OOM)", ["Убит: php-fpm7.4 (пул idv)", "Перезапустились: mysql, php7.4-fpm"])
    # служба не поднялась - это важнее
    assert oom.episode_short(e, timezone.utc, {"mysql.service"})[1][1] == "Не поднялась: mysql"
    # процесс службы: имя службы, только если оно что-то добавляет
    b = _block([_kill("mysqld", memcg="/system.slice/mysql.service", anon_mb=500),
                _kill("java", memcg="/system.slice/tomcat9.service", anon_mb=900, dt=299),
                _kill("node", memcg="/system.slice/docker-" + "ab" * 32 + ".scope", anon_mb=300, dt=298)])
    e = oom.episodes(oom.records(b))[-1]
    assert oom.episode_short(e, timezone.utc, set(), {"ab" * 6: "web"})[1] == [
        "Убиты: java (tomcat9), mysqld, node (контейнер web)"]


def test_oom_late_and_counter_only():
    rep = _oom_rep(HOST_BLOCK)
    s = SimpleNamespace(oom_total=1, oom_victim="", last_report=rep, alert_mutes=[])
    late = NOW + timedelta(hours=2)
    rep_late = dict(rep, clock_unix=late.timestamp(), extras={"oom": dict(HOST_BLOCK, ts=late.timestamp() - 20)})
    _fire, done, _ = oom.alert_step(s, {"oom_end": 0.0}, rep_late, late, timezone.utc, set())
    assert done["oom_short"][0] == "Кончилась память (OOM) в " + datetime.fromtimestamp(TS - 300, timezone.utc).strftime("%H:%M")
    # helper'а нет: по счетчику ядра, жертва без pid
    bare = {"clock_unix": TS}
    s = SimpleNamespace(oom_total=2, oom_victim="java (pid 7)", last_report=bare, alert_mutes=[])
    fire, done, _ = oom.alert_step(s, {"oom_seen": 0}, bare, NOW, timezone.utc)
    assert fire[0] == 1 and done["oom_short"] == ("Кончилась память (OOM)", ["Убит: java"])
    assert oom.counter_short("") == ("Кончилась память (OOM)", [])


# -- рост памяти --

def _hist(vals, last_day=date(2026, 10, 11)):
    days = [last_day - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))]
    return {"d": "2026-10-12", "m": vals[-1], "h": [[d.isoformat(), v] for d, v in zip(days, vals)]}


def test_mem_leak():
    st = {"mem_trend": {"pod:access-hub/access-hub-5965758d6c-tbvbr": _hist([746, 790, 835, 880, 925])}}
    s = _srv(alert_state=st, last_report={"mem_total": 2 * 2**30, "mem_used": 2 * 2**30 - 50 * MB})
    items = [x for x in mem_trend.problems(s, NOW) if x["level"]]
    assert _clean(*mem_trend.tg_short(items)) == ("Память растет 4 дня", ["Под: access-hub, уже 925мб"])
    st["mem_trend"].update({"docker:rest-server": _hist([87, 128, 170, 211, 253]),
                            "pod:shop/api-7f9c8d6b5-x2k4q": _hist([300, 350, 400, 450, 500])})
    items = [x for x in mem_trend.problems(_srv(alert_state=st, last_report=s.last_report), NOW) if x["level"]]
    what, facts = mem_trend.tg_short(items)
    assert what == "Память растет 4 дня" and len(facts) == 2 and facts[1].startswith("Еще: ")
    assert mem_trend.tg_short([{"key": "proc:dockerd@root", "kind": "proc", "name": "dockerd@root",
                                "series": [513 * MB, 703 * MB]}]) == ("Память растет 4 дня", ["Процесс: dockerd, уже 703мб"])
    assert mem_trend.tg_short([]) is None


# -- частые перезапуски --

def _pod(ns, name, ctrl, last=None, node="k8s-a"):
    p = {"ns": ns, "name": name, "owner": "ReplicaSet", "ctrl": f"deployment/{ctrl}", "phase": "Running",
         "ready": True, "restarts": 9, "node": node}
    if last:
        p["last"] = last
    return p


def _ev(*minutes_ago):
    return {"rc": 9, "ev": [[(NOW - timedelta(minutes=m)).isoformat(), 1] for m in minutes_ago]}


def test_kube_restarts_pod_with_cause():
    log = ('{"time":"2026-10-21 02:05:59.686764 +0000","level":"error","message":"PDOException: SQLSTATE[HY000]: '
           'General error: 7 no connection to the server"}')
    pods = [_pod("tech1", "scheduler-dispatch-5d84ccdc5-pmgp5", "scheduler-dispatch",
                 {"c": "app", "r": "Error", "x": 1, "log": log})]
    s = _srv(last_report={"kube": {"pods": pods}},
             alert_state={"restarts": {"pod:tech1/scheduler-dispatch-5d84ccdc5-pmgp5": _ev(3, 60, 120, 200, 300, 400)}})
    items = [x for x in restarts.problems(s, NOW) if x["kind"] == "pod"]
    assert _clean(*restarts.tg_short(items)) == ("Под перезапускается", ["tech1/scheduler-dispatch: нет связи с базой"])
    two = items + [{"key": "pod:default/portainer", "kind": "pod", "name": "default/portainer", "n": 5,
                    "text": "под default/portainer-6dd6ddbf5f-blkvc перезапускался 5 раз за сутки, последний 0 мин назад"}]
    assert restarts.tg_short(two) == ("Поды перезапускаются", ["tech1/scheduler-dispatch: нет связи с базой",
                                                                "Еще: portainer"])
    assert restarts.tg_short([]) is None


def test_kube_restarts_bursts():
    pods = [_pod("default", "app-a-6b8b56c769-f4k8m", "app-a", {"c": "m", "r": "Error", "x": 1, "log": "leader election lost"}),
            _pod("default", "app-b-5d88689b5d-rg5d7", "app-b", {"c": "frr", "r": "Error", "x": 143, "probe": "liveness"}),
            _pod("default", "app-c-649ccd4dfd-zz4k8", "app-c")]
    stall = [30 + 120 * i for i in range(3)]
    rs = {"pod:default/app-a-6b8b56c769-f4k8m": _ev(*stall), "pod:default/app-b-5d88689b5d-rg5d7": _ev(*stall),
          "pod:default/app-c-649ccd4dfd-zz4k8": _ev(*stall)}
    items = restarts.problems(_srv(last_report={"kube": {"pods": pods}}, alert_state={"restarts": rs}), NOW)
    assert _clean(*restarts.tg_short(items)) == ("Поды перезапускаются пачками, подвисает нода", ["app-a, app-b, app-c"])


def test_docker_restarts_and_causes():
    one = {"key": "docker:mcp-gateway", "kind": "docker", "name": "mcp-gateway", "n": 15,
           "text": "контейнер mcp-gateway перезапускался 15 раз за сутки, последний 15 мин назад"}
    assert restarts.tg_short([one]) == ("mcp-gateway часто перезапускается", ["15 раз за сутки"])
    two = [one, dict(one, key="docker:redis", name="redis", n=6)]
    assert restarts.tg_short(two) == ("mcp-gateway часто перезапускается", ["15 раз за сутки", "Еще: redis"])
    cs = restarts.cause_short
    assert cs("OOMKilled при лимите 512Mi") == "OOM при лимите 512мб"
    assert cs("OOMKilled, лимита нет: кончилась память ноды") == "OOM, кончилась память ноды"
    assert cs("убит по liveness-пробе (Get http://10.0.0.5:8080/health: context deadline exceeded)") == "не прошел liveness-пробу"
    assert cs("exit 137 (SIGKILL)") == "exit 137 (SIGKILL)"
    assert cs("exit 1: dial tcp 10.0.0.7:6379: connect: connection refused") == "нет связи с Redis"
    assert cs("exit 2: something odd happened") == "exit 2"


# -- GPU и LLM --

def _gpu_rep(gpus, xid=(), pci=()):
    return {"clock_unix": TS, "extras": {"gpu": {"v": 1, "ts": TS - 30, "smi": True, "ok": True, "gpus": list(gpus),
                                                 "xid": list(xid), "pci": list(pci)}}}


def test_gpu_problems():
    rep = _gpu_rep([{"i": 0, "bus": "00000000:41:00.0"}, {"i": 1, "bus": "00000000:42:00.0"}],
                   xid=[{"code": 79, "ts": TS - 60, "bus": "PCI:0000:41:00"}, {"code": 48, "ts": TS - 90, "bus": "PCI:0000:42:00"}])
    items = gpu.problems(rep, NOW)
    assert _clean(*gpu.tg_short(items)) == ("Отвалилась GPU 0", ["Xid 79", "Еще: ошибки памяти GPU 1"])
    assert gpu.tg_short(items[1:]) == ("Ошибки памяти GPU 1", ["Xid 48, нужен сброс GPU или перезагрузка"])
    # карта пропала с шины: номера нет, есть адрес
    lost = _gpu_rep([], xid=[{"code": 79, "ts": TS - 60, "bus": "PCI:0000:41:00"}],
                    pci=[{"bus": "0000:41:00.0", "drv": "nvidia"}])
    assert gpu.tg_short(gpu.problems(lost, NOW)) == ("Отвалилась GPU 0000:41:00.0", ["Xid 79, nvidia-smi ее не видит"])
    drv = {"clock_unix": TS, "extras": {"gpu": {"v": 1, "ts": TS - 30, "smi": True, "ok": False, "mod": False,
                                                "mod_built": False, "kernel": "6.8.0-48"}}}
    assert gpu.tg_short(gpu.problems(drv, NOW)) == ("Драйвер NVIDIA не отвечает", ["модуля nvidia нет для ядра 6.8.0-48"])
    assert gpu.tg_short([]) is None


def test_gpu_heat():
    hot = _gpu_rep([{"i": 1, "temp": 89, "slow": 87, "thr": {"sw": True}, "clk": 1200, "clkm": 1800}])
    assert _clean(*gpu.tg_short_heat(gpu.heat(hot, NOW))) == ("GPU 1 греется до 89°C", ["частота снижена до 1200 из 1800 МГц"])
    warm = _gpu_rep([{"i": 1, "temp": 84, "slow": 87}])
    assert gpu.tg_short_heat(gpu.heat(warm, NOW)) == ("GPU 1 греется до 84°C", [])
    assert gpu.tg_short_heat([gpu.heat(warm, NOW)]) == ("GPU 1 греется до 84°C", [])
    assert gpu.tg_short_heat((0, "")) is None


def test_llm():
    k = "vllm|container|Qwen3.8-27B|8000"
    slow = (1, "vLLM Qwen3.8-27B: задержка до первого токена p95 2.4 с, обычно 488 мс, держится 20 мин при 120 "
               "запросах за 5 минут", f"llm:ttft:{k}")
    assert _clean(*gpu.tg_short_llm([slow])) == ("LLM отвечает медленно", ["Qwen3.8-27B: первый токен 2.4 с вместо 0.5"])
    down = (2, "TEI embed: не отвечает на /metrics (порт закрыт), процесс работает 2 ч", "llm:down:tei|container|embed|80")
    tpot = (1, "vLLM Qwen3.8-27B: задержка на токен p95 310 мс, обычно 120 мс, держится 15 мин при 90 запросах "
               "за 5 минут", f"llm:tpot:{k}")
    assert gpu.tg_short_llm([down, slow, tpot]) == ("LLM не отвечает", ["embed: порт закрыт", "Еще: Qwen3.8-27B"])
    assert gpu.tg_short_llm([tpot])[1] == ["Qwen3.8-27B: на токен 310 мс вместо 120"]
    assert gpu.tg_short_llm([]) is None


# -- веб: сертификаты и конфиги --

def _cert(name, days, file=None):
    return {"where": "host", "file": file or f"/etc/letsencrypt/live/{name}/fullchain.pem", "names": name,
            "end": TS + days * 86400}


def test_web_cert():
    rep = {"extras": {"web-certs": {"v": 1, "ts": TS, "renew": "none", "certs": [
        _cert("admin.example.com", -382), _cert("bp.example.com", -382),
        _cert("example.org www.example.org", -79, "/etc/letsencrypt/live/example.org/fullchain.pem")]}}}
    assert _clean(*web_certs.tg_short(web_certs.problems(rep, NOW))) == (
        "Истекли сертификаты, продление не настроено",
        ["admin.example.com, bp.example.com, example.org"])
    soon = {"extras": {"web-certs": {"v": 1, "ts": TS, "certs": [_cert("artportret.ru", 5.5, "/home/lem/ssl/a.pem")]}}}
    assert web_certs.tg_short(web_certs.problems(soon, NOW)) == ("Сертификат истекает через 5 дней", ["artportret.ru"])
    assert web_certs.tg_short([]) is None


def test_web_config():
    err = '2026/10/12 20:00:37 [emerg] 56427#56427: host not found in upstream "books-php7.4-fpm" in /etc/nginx/conf.d/site.conf:34'
    rep = {"extras": {"web-config": {"v": 1, "ts": TS, "items": [
        {"server": "nginx", "where": "test-nginx", "ok": False, "err": err},
        {"server": "nginx", "where": "max-vote-nginx-1", "ok": False,
         "err": "Error response from daemon: container be15873254ba is not running"}]}}}
    items = web_config.problems(rep, NOW)
    assert _clean(*web_config.tg_short(items)) == (
        "Конфиг nginx сломан, reload уронит сайты", ["test-nginx: нет upstream books-php7.4-fpm", "Еще: max-vote-nginx-1"])
    assert web_config.tg_short(items[1:])[1] == ["max-vote-nginx-1: контейнер не запущен"]
    host = {"extras": {"web-config": {"v": 1, "ts": TS, "items": [
        {"server": "apache", "where": "host", "ok": False, "err": "AH00526: Syntax error on line 12 of /etc/apache2/sites-enabled/x.conf"}]}}}
    what, facts = web_config.tg_short(web_config.problems(host, NOW))
    assert what == "Конфиг Apache сломан, reload уронит сайты" and facts[0].startswith("AH00526: Syntax error")
    assert web_config.tg_short([]) is None


# -- прогноз диска и прирост бэкапа --

def test_disk_forecast():
    items = [{"mount": "/", "kind": "space", "pct": 69.0, "rate": 31.0, "eta_h": 21.0}]
    assert _clean(*disk_forecast.tg_short(items)) == ("Диск / заполнится через 21 ч", ["сейчас 69%"])
    more = items + [{"mount": "/var", "kind": "inode", "pct": 80.4, "rate": 5.0, "eta_h": 72.0},
                    {"mount": "/backup", "kind": "space", "pct": 40.0, "rate": 1.0, "eta_h": 400.0}]
    assert disk_forecast.tg_short(more) == ("Диск / заполнится через 21 ч", ["сейчас 69%", "Еще: inode /var через 3 дня"])
    assert disk_forecast.tg_short(more[1:2]) == ("Inode на /var кончатся через 3 дня", ["сейчас 80%"])
    assert disk_forecast.tg_short([]) is None and disk_forecast.tg_short(None) is None


def test_backup_growth():
    runs = [{"ts": TS - (20 - i) * 86400, "st": 54 * MB} for i in range(14)] + [{"ts": TS - 3 * 3600, "st": 76.1 * 2**30}]
    rep = {"extras": {"backup-runs": {"v": 1, "ts": TS, "runs": runs}}}
    g = backup_growth.jump(SimpleNamespace(last_report=rep), NOW)
    assert _clean(*backup_growth.tg_short(g)) == ("Бэкап вырос на 76гб за раз", ["обычно 54мб"])
    assert backup_growth.tg_short([g]) == backup_growth.tg_short(g)
    assert backup_growth.tg_short(None) is None
