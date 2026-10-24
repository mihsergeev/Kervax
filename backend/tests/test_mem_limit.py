"""У своего лимита памяти: под или контейнер молча тормозит у limit (app/mem_limit.py, агент 2.51).

Числа - с живых нод (только чтение, 24.10.2026): под базы у лимита 384Mi - занято 99.9%, PSI full
avg60 2.6-5, major faults 87-1340/с, refault файлов десятки тысяч в секунду; поды по 97% лимита,
у которых это кэш (anon 20-50%), - давление и faults по нулям."""
from datetime import datetime, timedelta, timezone

from app import collector, mem_limit, srv_short
from app.models import Server

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=timezone.utc)
MI = 2**20
BAD = "—–≥↳«»·…ёЁ"

THRASH = {"kind": "pod", "name": "default/wp-mariadb-0", "u": "386fef04", "lim": 384 * MI, "cur": 383 * MI,
          "anon": 370 * MI, "file": 10 * MI, "some": 5.0, "full": 4.8, "f10": 5.2, "mf": 87, "rf": 44650}
# кэш заполнил лимит: так у многих, это нормально
CACHE = {"kind": "pod", "name": "shop/api-7f9c8d6b5-x2k4q", "u": "f3224e2d", "lim": 256 * MI, "cur": 248 * MI,
         "anon": 55 * MI, "file": 190 * MI, "some": 0, "full": 0, "f10": 0, "mf": 0, "rf": 0}


def _rep(*items, **kw):
    r = {"mem_limits": list(items),
         "kube": {"access": True, "pods": [
             {"ns": "default", "name": "wp-mariadb-0", "u": "386fef04", "ctrl": "statefulset/wp-mariadb"},
             {"ns": "shop", "name": "api-7f9c8d6b5-x2k4q", "u": "f3224e2d", "ctrl": "deployment/api"}]}}
    r.update(kw)
    return r


def _srv(state=None, mutes=None, rep=None, seen=NOW):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=seen,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=_rep() if rep is None else rep,
                  alert_state={"memlim": state} if state else {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _run(reps, start=NOW, step_s=60, state=None):
    """Тики цикла алертов раз в step_s: на каждом - свой отчет. Возвращает состояние и время конца."""
    t = start
    for rep in reps:
        state = mem_limit.update(state, rep, t)
        t += timedelta(seconds=step_s)
    return state, t - timedelta(seconds=step_s)


def test_hot_thresholds():
    assert mem_limit.hot(THRASH)
    assert not mem_limit.hot(CACHE)  # у лимита, но давления нет: кэш
    # PSI full хватает одного; major faults и refault - только вместе с PSI some
    assert mem_limit.hot({**CACHE, "full": 1.0})
    assert mem_limit.hot({**CACHE, "some": 5, "mf": 50})
    assert mem_limit.hot({**CACHE, "some": 5, "rf": 2000})
    assert not mem_limit.hot({**CACHE, "mf": 50})
    assert not mem_limit.hot({**CACHE, "rf": 2000})
    assert not mem_limit.hot({**CACHE, "some": 4.9, "rf": 50000})
    assert not mem_limit.hot({**CACHE, "full": 0.9, "some": 5, "mf": 49, "rf": 1999})
    # давление есть, но до лимита далеко: это не лимит (память сервера, своп) - не наше
    assert not mem_limit.hot({**THRASH, "cur": 300 * MI})


def test_old_agent_untouched():
    assert mem_limit.block({}) is None
    assert mem_limit.update({"pod:x/y": {"s": 1, "l": 2}}, {}, NOW) is None
    assert mem_limit.block({"mem_limits": []}) == []
    assert mem_limit.view(_srv(rep={}), NOW) is None


def test_debounce_ten_minutes():
    # 9 минут у лимита - еще нет
    st, t = _run([_rep(THRASH)] * 10)
    assert mem_limit.problems(_srv(st), t) == []
    # 10 минут - есть, ключ по ворклоаду
    st, t = _run([_rep(THRASH)] * 11)
    p = mem_limit.problems(_srv(st), t)
    assert [x["key"] for x in p] == ["pod:default/wp-mariadb"] and p[0]["level"] == 1
    assert p[0]["text"] == ("под default/wp-mariadb уперся в лимит памяти 384Mi: используется 99%, PSI full 4.8, "
                            "major faults 87/с - тормозит, но не падает; поднимите limit памяти пода")


def test_spike_is_not_alert():
    # прогрев после старта: 5 минут бьется о лимит, потом успокоился - так и не алерт
    st, t = _run([_rep(THRASH)] * 5 + [_rep(CACHE)] * 30)
    assert mem_limit.problems(_srv(st), t) == []
    v = mem_limit.view(_srv(st, rep=_rep(CACHE)), t)
    assert v["bad"] == [] and v["past"] == []  # спайк не эпизод
    assert [x["name"] for x in v["near"]] == ["shop/api"] and not v["near"][0]["hot"]


def test_short_gaps_keep_episode():
    # пауза в нагрузке на 2 минуты эпизод не рвет: 6 + 2 + 6 минут - это 14 минут подряд
    st, t = _run([_rep(THRASH)] * 6 + [_rep()] * 2 + [_rep(THRASH)] * 6)
    assert len(mem_limit.problems(_srv(st), t)) == 1
    # а пауза на 5 минут - рвет: две части по 6 минут
    st, t = _run([_rep(THRASH)] * 6 + [_rep()] * 5 + [_rep(THRASH)] * 6)
    assert mem_limit.problems(_srv(st), t) == []


def test_cooldown_no_flapping():
    st, t = _run([_rep(THRASH)] * 15)
    assert len(mem_limit.problems(_srv(st), t)) == 1
    # затихло: 14 минут держим (один эпизод, без отбоя и нового алерта), на 16-й - отбой
    st2, t2 = _run([_rep()] * 14, start=t + timedelta(minutes=1), state=st)
    assert len(mem_limit.problems(_srv(st2), t2)) == 1
    # снова пошло внутри остывания - тот же эпизод, начало прежнее
    st3, t3 = _run([_rep(THRASH)], start=t2 + timedelta(minutes=1), state=st2)
    assert st3["pod:default/wp-mariadb"]["s"] == st["pod:default/wp-mariadb"]["s"]
    st4, t4 = _run([_rep()] * 17, start=t + timedelta(minutes=1), state=st)
    assert mem_limit.problems(_srv(st4), t4) == []
    # прошедший эпизод виден в карточке сутки
    v = mem_limit.view(_srv(st4, rep=_rep()), t4)
    assert [x["key"] for x in v["past"]] == ["pod:default/wp-mariadb"]
    st5, _ = _run([_rep()], start=t + timedelta(days=1, minutes=1), state=st4)
    assert st5 == {}


def test_pod_restart_same_key_and_uid_names():
    # под пересоздали (новое имя деплоймента) - тот же ворклоад, эпизод продолжается
    a = {**THRASH, "kind": "pod", "name": "shop/api-7f9c8d6b5-x2k4q"}
    b = {**THRASH, "kind": "pod", "name": "shop/api-6b8b56c769-f4k8m"}
    st, t = _run([_rep(a)] * 6 + [_rep(b)] * 6)
    assert [x["key"] for x in mem_limit.problems(_srv(st), t)] == ["pod:shop/api"]
    # воркер без kube-api: под по uid, имя - из отчета контроллера кластера
    w = {**THRASH, "name": "pod 386fef04"}
    st = mem_limit.update(None, {"mem_limits": [w]}, NOW, {"386fef04": "default/wp-mariadb-0"})
    assert list(st) == ["pod:default/wp-mariadb"]
    st = mem_limit.update(None, {"mem_limits": [w]}, NOW)
    assert list(st) == ["pod:?/pod 386fef04"]


def test_container_and_unit_text_and_level():
    c = {**THRASH, "kind": "container", "name": "shop-db", "lim": 512 * MI, "cur": 510 * MI, "full": 12, "mf": 1340}
    u = {**THRASH, "kind": "unit", "name": "php8.3-fpm.service", "lim": 5 * 2**30, "cur": 5 * 2**30, "mf": 0, "rf": 3000}
    st, t = _run([_rep(c, u)] * 11)
    p = {x["key"]: x for x in mem_limit.problems(_srv(st), t)}
    assert p["docker:shop-db"]["level"] == 2  # PSI full от 10% - авария
    assert "лимит памяти 512Mi: используется 99%, PSI full 12, major faults 1340/с" in p["docker:shop-db"]["text"]
    assert p["docker:shop-db"]["text"].endswith("поднимите лимит памяти контейнера (mem_limit)")
    assert "юнит php8.3-fpm.service уперся в лимит памяти 5Gi" in p["unit:php8.3-fpm.service"]["text"]
    assert "refault 3000/с" in p["unit:php8.3-fpm.service"]["text"]
    assert mem_limit.lim_text(400_000_000) == "381мб"


def test_decided_and_mute_by_workload():
    st, t = _run([_rep(THRASH)] * 11)
    s = _srv(st, mutes=["memlim:pod:default/wp-mariadb"])
    assert mem_limit.problems(s, t) == []
    assert [x["decided"] for x in mem_limit.problems(s, t, with_decided=True)] == [True]
    # мьют, поставленный на под (со старых времен или из ленты), глушит ворклоад
    s = _srv(st, mutes=["memlim:pod:default/wp-mariadb-0"])
    assert mem_limit.problems(s, t) == []
    assert collector._sig_all_quiet(s, "mem_limit", {"pod:default/wp-mariadb"}, t)
    assert not collector._sig_all_quiet(_srv(st), "mem_limit", {"pod:default/wp-mariadb"}, t)


def test_collector_wiring():
    st, t = _run([_rep(THRASH, CACHE)] * 11)
    s = _srv(st, rep=_rep(THRASH, CACHE), seen=t)
    lvl, ctx = collector._server_conditions(s, t)["mem_limit"]
    assert lvl == 1 and ctx["sig"] == ["pod:default/wp-mariadb"] and "384Mi" in ctx["detail"]
    rows = [(p["level"], p["sec"], p["mute"]) for p in collector.server_problems(s, t) if p["kind"] == "mem_limit"]
    assert rows == [(1, "memlimit", "memlim:pod:default/wp-mariadb")]
    # старый агент: условия нет вовсе (молчание не значит "в норме")
    assert "mem_limit" not in collector._server_conditions(_srv(st, rep={}, seen=t), t)
    assert collector._recovery_detail("mem_limit", {}, {}).startswith("контейнеры и поды больше не упираются")


def test_tg_short():
    st, t = _run([_rep(THRASH)] * 11)
    items = mem_limit.problems(_srv(st), t)
    what, facts = srv_short.short("mem_limit", {"items": items}, None)
    assert what == "Под default/wp-mariadb уперся в лимит памяти 384Mi"
    assert facts == ["Используется 99%, PSI full 4.8, major faults 87/с - тормозит, но не падает"]
    assert not any(c in what + "".join(facts) for c in BAD)
    assert srv_short.icon("mem_limit", 1) == "⚠️🧠"
    c = {**THRASH, "kind": "container", "name": "shop-db"}
    st, t = _run([_rep(THRASH, c)] * 11)
    _what, facts = srv_short.short("mem_limit", {"items": mem_limit.problems(_srv(st), t)}, None)
    assert facts[1] == "Еще: default/wp-mariadb" or facts[1] == "Еще: shop-db"


# ClickHouse у лимита 40Gi (вечер 24.10): занято 99.7%, анонимной памяти около четверти, кэш
# страниц по кругу - refault волнами 4-11 тыс/с, а PSI и major faults по нулям. Было четыре пары
# "уперся" - "больше не упирается" за вечер
GI = 2**30
CH = {"kind": "pod", "name": "default/chi-olap-main-0-0-0", "u": "5e1fa0c2", "lim": 40 * GI, "cur": int(39.88 * GI),
      "anon": int(9.25 * GI), "file": int(30.5 * GI), "some": 0, "full": 0, "f10": 0, "mf": 0, "rf": 0}


def _ch_evening():
    """Вечер по минутам: refault волнами по 20-30 минут с перерывами в час-два."""
    out = []
    for burst, quiet in ((26, 150), (24, 35), (25, 34), (26, 40)):
        out += [_rep({**CH, "rf": 3700 + 300 * (i % 25)}) for i in range(burst)]
        out += [_rep(CH)] * quiet
    return out


def test_page_cache_churn_at_limit_is_not_thrash():
    st, t = _run(_ch_evening())
    assert mem_limit.problems(_srv(st), t) == []
    v = mem_limit.view(_srv(st, rep=_rep(CH)), t)
    assert v["bad"] == [] and v["past"] == [] and [x["key"] for x in v["near"]] == ["pod:default/chi-olap-main-0-0"]


def test_same_waves_with_memory_pressure_alert():
    # та же база, но ей тесно: refault вместе с давлением - процессы ждут память
    reps = [_rep({**CH, "some": 7.5, "rf": 9000, "mf": 12})] * 12
    st, t = _run(reps)
    p = mem_limit.problems(_srv(st), t)
    assert len(p) == 1 and "PSI some 7.5, major faults 12/с" in p[0]["text"]
