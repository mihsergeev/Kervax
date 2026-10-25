"""OOM по лимиту cgroup и OOM всего сервера - разные события, и тексты у них разные. Процесс,
уперся в memory.max своего контейнера, пода или службы, ядро убивает, даже если на сервере память
свободна: писать про такое "кончилась память" - вводить в заблуждение. Вид берется из журнала ядра
(helper units-setup: "Memory cgroup out of memory" или constraint=CONSTRAINT_MEMCG - kind memcg)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app import oom, tg_fmt

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TS = NOW.timestamp()
CID = "ab12" * 16
CT_CG = f"/system.slice/docker-{CID}.scope"
SVC_CG = "/system.slice/app-api.service"
BAD = "—–≥↳«»·…ёЁ"


def _kill(name, kind, memcg, anon_mb, limit_kb=None, oom_memcg=None, dt=300):
    return {"ts": TS - dt, "pid": 100 + dt, "name": name, "kind": kind, "memcg": memcg,
            "oom_memcg": memcg if oom_memcg is None else oom_memcg, "uid": 0, "user": "root",
            "anon": anon_mb * 1024, "mem": {"limit": limit_kb} if limit_kb else {"ram": 8 * 2**20, "swap_total": 0},
            "top": [{"name": name, "uid": 0, "user": "root", "n": 1, "anon": anon_mb * 1024}]}


def _rep(kills):
    return {"clock_unix": TS, "extras": {"oom": {"v": 1, "ts": TS - 20, "kills": kills, "units": []}},
            "docker": {"containers": [{"id": CID[:12], "name": "app-worker-1", "state": "running"}]}}


def _ep(rep):
    return oom.episodes(oom.records(rep["extras"]["oom"]))[-1]


def _clean(text):
    assert not any(c in text for c in BAD), text
    return text


def test_limit_oom_says_limit_not_server_memory():
    rep = _rep([_kill("python", "memcg", CT_CG, 4000, 4 * 2**20)])
    e, names = _ep(rep), oom.names_of(rep)
    what, facts = oom.episode_short(e, timezone.utc, set(), names)
    assert (what, facts) == ("Убит по лимиту памяти (OOM)", ["python - контейнер app-worker-1 уперся в свой лимит 4гб"])
    assert _clean(tg_fmt.render("⚠️🧠", "node", what, facts)) == (
        "⚠️🧠 node\n<b>Убит по лимиту памяти (OOM)</b>\npython - контейнер app-worker-1 уперся в свой лимит 4гб")
    # полный текст (история алертов): лимит, память сервера ни при чем, и что сделать
    lines = oom.episode_text(e, timezone.utc, set(), 1, names).split("\n")
    assert lines[0].startswith("OOM по лимиту памяти контейнера app-worker-1, ")
    assert "лимит 4гб, память сервера тут ни при чем" in lines
    assert lines[-1] == ("что сделать: поднять лимит (mem_limit или deploy.resources.limits.memory в compose) "
                         "или ограничить память в самом приложении (меньше воркеров и кэшей, -Xmx у java)")
    assert not any("кончилась" in x or "нехватка" in x for x in lines)
    # главная
    s = SimpleNamespace(last_report=rep, alert_mutes=[], alert_snoozes={})
    assert oom.problem(s, NOW) == (
        1, "OOM по лимиту памяти контейнера app-worker-1 (4гб): был 5 мин назад, убиты python 3.9гб", "oom:ct:app-worker-1")
    # карточка: чей лимит, чтобы подсказать, где его поднять
    assert oom.episode_view(e, names)["limit_owner"] == {"kind": "container", "name": "app-worker-1", "unknown": False}


def test_server_oom_says_server():
    rep = _rep([_kill("mysqld", "global", "/system.slice/mysql.service", 3000)])
    e, names = _ep(rep), oom.names_of(rep)
    what, facts = oom.episode_short(e, timezone.utc, set(), names)
    assert what == "Кончилась память на сервере (OOM)" and facts == ["Убит: mysqld"]
    text = oom.episode_text(e, timezone.utc, set(), 1, names)
    assert text.startswith("нехватка памяти на сервере (OOM), ") and "RAM 8гб, swap нет" in text
    assert "лимит" not in text and "что сделать" not in text
    s = SimpleNamespace(last_report=rep, alert_mutes=[], alert_snoozes={})
    assert oom.problem(s, NOW)[1].startswith("нехватка памяти на сервере (OOM) из-за mysql.service: была 5 мин назад")
    assert oom.episode_view(e, names)["limit_owner"] is None


def test_limit_oom_service_several_killed_and_no_limit_value():
    # служба уперлась в MemoryMax, убиты двое: заголовок во множественном, род по владельцу лимита
    rep = _rep([_kill("node", "memcg", SVC_CG, 900, 1 * 2**20),
                _kill("worker", "memcg", SVC_CG + "/sub.scope", 300, 1 * 2**20, oom_memcg=SVC_CG, dt=299)])
    e = _ep(rep)
    assert oom.episode_short(e, timezone.utc, set()) == (
        "Убиты по лимиту памяти (OOM)", ["node, worker - служба app-api уперлась в свой лимит 1гб"])
    assert "MemoryMax= в юните" in oom.episode_text(e, timezone.utc, set(), 1)
    # лимита в журнале нет - без числа
    rep = _rep([dict(_kill("node", "memcg", SVC_CG, 900), mem={})])
    assert oom.episode_short(_ep(rep), timezone.utc, set())[1] == ["node - служба app-api уперлась в свой лимит памяти"]


def test_kind_from_helper_and_counter_without_journal():
    # нет поля kind (или чужое значение) - как раньше, весь сервер; вид дает только журнал ядра
    rec = oom.records({"v": 1, "ts": TS, "kills": [{"ts": TS, "name": "x", "kind": "?"}], "units": []})
    assert rec[0]["kind"] == "global"
    # только счетчик ядра (oom_kill считает и лимиты cgroup): какой OOM, не знаем - без "на сервере"
    assert oom.counter_short("java (pid 7)") == ("Кончилась память (OOM)", ["Убит: java"])
