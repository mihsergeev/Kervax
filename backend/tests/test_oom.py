"""OOM killer: разбор журнала ядра в helper'е units-setup 0.5, эпизоды, алерт "нехватка памяти",
привязка упавших юнитов к эпизоду, счетчик ядра от агента 2.42.

Журнал в tests/fixtures/oom-journal.txt - настоящий вывод ядра 6.8 с двух эпизодов на одном сервере,
обезличенный: имя хоста, id контейнеров, лишние строки таблицы процессов убраны."""
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import oom
from app.alerts import full_text

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "units-setup.sh").read_text(encoding="utf-8")
JOURNAL = (Path(__file__).parent / "fixtures" / "oom-journal.txt").read_text(encoding="utf-8")
T0 = 1791901981.5  # первое убийство в журнале - около него
T1 = 1792488141.7  # последнее

# лимит cgroup (systemd-run --scope -p MemoryMax=200M), убийство systemd-oomd, ядро старше 6.x
# (в таблице нет rss_anon) с обрезанной таблицей и именем процесса с пробелом
EXTRA = """1792490000.100000 web-a kernel: python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=0
1792490000.100100 web-a kernel: memory: usage 204800kB, limit 204800kB, failcnt 37
1792490000.100200 web-a kernel: Tasks state (memory values in pages):
1792490000.100300 web-a kernel: [  pid  ]   uid  tgid total_vm      rss rss_anon rss_file rss_shmem pgtables_bytes swapents oom_score_adj name
1792490000.100400 web-a kernel: [   9001]     0  9001   140000    51000    50900      100         0   450000        0             0 python3
1792490000.100500 web-a kernel: oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,oom_memcg=/system.slice/run-r1a2b3c.scope,task_memcg=/system.slice/run-r1a2b3c.scope,task=python3,pid=9001,uid=0
1792490000.100600 web-a kernel: Memory cgroup out of memory: Killed process 9001 (python3) total-vm:560000kB, anon-rss:203600kB, file-rss:400kB, shmem-rss:0kB, UID:0 pgtables:440kB oom_score_adj:0
1792491000.000000 web-a systemd-oomd[700]: Killed /user.slice/user-1001.slice/user@1001.service/app.slice/app-build.scope due to memory pressure for /user.slice/user-1001.slice/user@1001.service being 81.20% > 50.00% for > 20s with reclaim activity
1792492000.000000 web-a kernel: java invoked oom-killer: gfp_mask=0x100cca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=0
1792492000.000100 web-a kernel: Tasks state (memory values in pages):
1792492000.000200 web-a kernel: [  pid  ]   uid  tgid total_vm      rss pgtables_bytes swapents oom_score_adj name
1792492000.000300 web-a kernel: [  1200]  1002  1200  2000000   900000  8000000        0             0 Web Content
1792492000.000400 web-a kernel: [  1201]  1002  1201  2000000
1792492000.000500 web-a kernel: Out of memory: Killed process 1200 (Web Content) total-vm:8000000kB, anon-rss:3590000kB, file-rss:10000kB, shmem-rss:0kB, UID:1002 pgtables:7800kB oom_score_adj:0
"""


def _body(name: str) -> str:
    return re.search(rf"<<'{name}'\n(.*?)\n{name}\n", SRC, re.S).group(1)


def _bash() -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    return bash


def _helper_json(journal: str, now: float, passwd: str = "web:x:1001:1001::/home/web:/bin/sh\n") -> dict:
    """Журнал через awk helper'а в kills.tsv, оттуда в oom.json - как в oom_scan."""
    bash = _bash()
    d = tempfile.mkdtemp()
    try:
        files = {"oom.awk": _body("OOM_AWK_EOF"), "json.awk": _body("OOM_JSON_EOF"), "j.txt": journal,
                 "passwd": passwd}
        for n, text in files.items():
            with open(os.path.join(d, n), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        script = ("set -eu\ncd \"$1\"\nexport LC_ALL=C\n"
                  "awk -v pg=4 -f oom.awk j.txt > p.tsv\n"
                  "grep -v '^C\t' p.tsv > k.tsv || true\n"
                  "sed -n 's/^C\t//p' p.tsv > cursor\n"
                  f"awk -F'\\t' -v now={int(now)} -v from=1 -v cut={int(now) - 7 * 86400} -v max=60 -f json.awk passwd k.tsv\n")
        sp = os.path.join(d, "run.sh")
        with open(sp, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        out = subprocess.run([bash, sp, d], capture_output=True, text=True, timeout=60, encoding="utf-8")
        assert out.returncode == 0, out.stderr
        with open(os.path.join(d, "cursor"), encoding="utf-8") as f:
            assert f.read().strip() == "s=0123;i=42"
        return json.loads(out.stdout)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_helper_version_and_wiring():
    assert "KERVAX_SETUP_VERSION=0.6" in SRC and "\r" not in SRC
    helper = _body("HELPER_EOF")
    assert 'scan) scan; oom_scan ;;' in helper
    # журнал читается одним запросом: ядро, отметки systemd о юнитах и systemd-oomd
    assert "_TRANSPORT=kernel + MESSAGE_ID=$OOM_MSGID + UNIT_RESULT=oom-kill + SYSLOG_IDENTIFIER=systemd-oomd" in helper
    # у каждого чтения журнала таймаут: под systemd пайп не останавливает journalctl
    assert helper.count("timeout 60 journalctl \"$@\"") == 2


def test_helper_parses_real_kernel_log():
    d = _helper_json(JOURNAL, T1 + 120)
    kills = d["kills"]
    assert [(k["name"], k["pid"]) for k in kills] == [
        ("mysqld", 826339), ("php-fpm7.4", 1587445), ("systemd", 424301), ("(sd-pam)", 424302), ("python", 477025)]
    py = kills[-1]
    assert py["memcg"] == "/system.slice/php7.4-fpm.service" and py["anon"] == 566956 and py["adj"] == 0
    assert py["uid"] == 1001 and py["user"] == "web" and py["kind"] == "global" and py["by"] == "kervax-agent"
    assert py["line"].startswith("Out of memory: Killed process 477025 (python) total-vm:945116kB")
    assert kills[2]["adj"] == 100 and kills[2]["memcg"].startswith("/user.slice/user-1001.slice/user@1001.service")
    # Mem-Info в килобайтах, swap нет; RAM - 2097017 страниц
    assert py["mem"] == {"anon": 7614200, "file": 5280, "shmem": 216648, "swap_free": 0, "swap_total": 0,
                         "ram": 8388068, "limit": None}
    # кто занимал память: сумма rss_anon по имени и uid, а не rss (у postgres он с общей памятью)
    top = {t["name"]: t for t in py["top"]}
    assert top["python"]["n"] == 12 and top["python"]["anon"] > 5_900_000 and top["python"]["user"] == "web"
    assert py["top"][0]["name"] == "python" and top["mysqld"]["n"] == 1
    units = [(u["unit"], u["what"]) for u in d["units"]]
    assert ("user@1001.service", "oom") in units and ("mysql.service", "failed") in units
    assert not any(u.endswith(".slice") for u, _ in units)


def test_helper_memcg_oomd_and_old_kernel():
    d = _helper_json(JOURNAL + EXTRA, 1792492100)
    memcg, oomd, old = d["kills"][-3:]
    assert memcg["kind"] == "memcg" and memcg["oom_memcg"] == "/system.slice/run-r1a2b3c.scope"
    assert memcg["mem"]["limit"] == 204800 and memcg["top"][0]["anon"] == 50900 * 4
    assert oomd["kind"] == "oomd" and oomd["memcg"].endswith("app-build.scope") and oomd["constraint"] == "pressure"
    # rss_anon нет - считаем по rss; обрезанная строка таблицы не роняет разбор; имя с пробелом целиком
    assert old["name"] == "Web Content" and old["top"] == [{"name": "Web Content", "uid": 1002, "n": 1, "anon": 3600000}]


def test_counter_step_survives_agent_restart():
    body = SimpleNamespace(oom_kill=0.0, oom_total=63, uptime_seconds=8000)
    # агент перезапустился посреди OOM: его дельта 0, а счетчик ядра вырос на 3
    assert oom.counter_step({"oom_total": 60, "uptime_seconds": 7900}, body) == (3, 3)
    # перезагрузка: счетчик с нуля
    assert oom.counter_step({"oom_total": 60, "uptime_seconds": 90000}, body) == (63, 63)
    # первый отчет со счетчиком и старый агент - дельта агента
    assert oom.counter_step({}, SimpleNamespace(oom_kill=2.0, oom_total=63, uptime_seconds=1)) == (2, 2)
    assert oom.counter_step({"oom_pend": 1}, SimpleNamespace(oom_kill=1.0, oom_total=None, uptime_seconds=1)) == (1, 2)


def _block(now: float, extra: str = "") -> dict:
    """oom.json из журнала, сдвинутый так, что последнее убийство было 5 минут назад."""
    d = _helper_json(JOURNAL + extra, T1 + 120)
    shift = now - 300 - T1
    for k in d["kills"] + d["units"]:
        k["ts"] = round(k["ts"] + shift, 6)
    d["ts"] = now - 20
    return d


def test_episodes_and_texts():
    now = 1_900_000_000.0
    b = _block(now)
    eps = oom.episodes(oom.records(b))
    assert len(eps) == 2
    first, last = eps
    assert [k["name"] for k in first["kills"]] == ["mysqld", "php-fpm7.4"]
    assert first["units"]["mysql.service"] == ["failed", "oom"]
    assert [k["name"] for k in last["kills"]] == ["systemd", "(sd-pam)", "python"]
    assert oom.kills_text(last) == "python 554мб (php7.4-fpm.service, uid web), systemd + sd-pam (сессия web)"
    assert oom.top_text(last).startswith("python x12 5.7гб (php7.4-fpm), mysqld 525мб")
    assert oom.mem_text(last) == "RAM 8гб, swap нет"
    # убит mysqld - критично; убиты воркер и сессия - предупреждение
    assert oom.level(first, set()) == 2 and oom.level(last, set()) == 1
    # служба упала и не поднялась - критично
    assert oom.level(last, {"php7.4-fpm.service"}) == 2
    text = oom.episode_text(last, timezone.utc, set(), 2)
    lines = text.split("\n")
    assert lines[0].startswith("нехватка памяти (OOM), ") and lines[1].startswith("убиты: python 554мб")
    assert "упали: php7.4-fpm.service (поднялась)" in text and "user@" not in text
    assert lines[-1] == "за 7 дней OOM 2 раз"


def test_owners():
    assert oom.owner("/system.slice/php7.4-fpm.service") == {"kind": "service", "name": "php7.4-fpm.service"}
    assert oom.owner("/system.slice/docker-" + "ab12" * 16 + ".scope")["kind"] == "container"
    assert oom.owner("/user.slice/user-1001.slice/session-4.scope", "web") == {"kind": "user", "name": "web", "uid": 1001}
    pod = "/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod1b2c3d4e_0000_1111_2222_333344445555.slice/cri-containerd-ab.scope"
    assert oom.owner(pod) == {"kind": "pod", "name": "1b2c3d4e-0000-1111-2222-333344445555"}
    assert oom.owner("/system.slice/run-r1a2b3c.scope") == {"kind": "scope", "name": "run-r1a2b3c.scope"}
    assert oom.owner("/")["kind"] == "host"
    assert oom.size_kb(566956) == "554мб" and oom.size_kb(8388068) == "8гб" and oom.size_kb(5991000) == "5.7гб"


def _server(rep: dict, **kw):
    return SimpleNamespace(oom_total=kw.get("total", 0), oom_victim=kw.get("victim", ""), last_report=rep,
                           alert_mutes=[])


def test_alert_step_lifecycle():
    """Первое знакомство с блоком - история молчит, свежий эпизод - алерт после затишья, повтор за
    час - откладывается, через час одно сообщение с количеством."""
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    b = _block(now.timestamp())
    # последний эпизод еще идет (последнее убийство меньше 90 секунд назад) - ждем
    young = copy.deepcopy(b)
    for k in young["kills"] + young["units"]:
        k["ts"] += 250
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": young}}
    fire, _done, now_apply = oom.alert_step(_server(rep, total=3), {}, rep, now, timezone.utc)
    assert fire is None and now_apply["oom_end"] < now.timestamp() - 900
    st = dict(now_apply)
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": b}}
    fire, done, _ = oom.alert_step(_server(rep, total=3), st, rep, now, timezone.utc)
    assert fire is not None and fire[0] == 1 and fire[1].startswith("нехватка памяти (OOM)")
    assert done["oom_seen"] == 3 and done["oom_sent"] == now.timestamp()
    st.update(done)
    # тот же эпизод второй раз не шлется, а счетчик агента, пришедший позже, - не новость
    fire, _d, upd = oom.alert_step(_server(rep, total=4), st, rep, now + timedelta(minutes=2), timezone.utc)
    assert fire is None and upd.get("oom_seen") == 4
    st.update(upd)
    # новый эпизод через 20 минут - откладывается до конца часа
    later = now + timedelta(minutes=20)
    b2 = copy.deepcopy(b)
    b2["kills"].append(dict(b2["kills"][-1], ts=later.timestamp() - 200, pid=5555))
    b2["ts"] = later.timestamp() - 10
    rep2 = {"clock_unix": later.timestamp(), "extras": {"oom": b2}}
    fire, _d, upd = oom.alert_step(_server(rep2, total=5), st, rep2, later, timezone.utc)
    assert fire is None and len(upd["oom_held"]) == 1
    st.update(upd)
    # через час после сообщения - одно сообщение про отложенное
    hour = now + timedelta(minutes=61)
    b2["ts"] = hour.timestamp() - 10
    rep3 = {"clock_unix": hour.timestamp(), "extras": {"oom": b2}}
    fire, done, _ = oom.alert_step(_server(rep3, total=5), st, rep3, hour, timezone.utc)
    assert fire is not None and done["oom_held"] == []


def test_alert_step_counter_without_details():
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    empty = {"v": 1, "ts": now.timestamp() - 20, "from": 1, "kills": [], "units": []}
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": empty}}
    st = {"oom_seen": 0, "oom_end": 0.0}
    # helper стоит: ждем журнал
    fire, _d, upd = oom.alert_step(_server(rep, total=2), st, rep, now, timezone.utc)
    assert fire is None and upd["oom_wait"] == now.timestamp()
    st.update(upd)
    later = now + timedelta(minutes=6)
    empty["ts"] = later.timestamp() - 20
    rep = {"clock_unix": later.timestamp(), "extras": {"oom": empty}}
    fire, done, _ = oom.alert_step(_server(rep, total=2, victim="java (pid 7)"), st, rep, later, timezone.utc)
    assert fire == (1, "нехватка памяти (OOM): ядро убило 2 процесс(ов), последний java (pid 7)")
    # helper'а нет вовсе - сразу
    rep = {"clock_unix": now.timestamp()}
    fire, _d, _u = oom.alert_step(_server(rep, total=1), {"oom_seen": 0}, rep, now, timezone.utc)
    assert fire is not None


def test_unit_link_and_quiet_units():
    now = 1_900_000_000.0
    eps = oom.episodes(oom.records(_block(now)))
    user = {"unit": "user@1001.service", "result": "signal", "status": 9, "since": now - 300,
            "log": ["user@1001.service: A process of this unit has been killed by the OOM killer."]}
    lk = oom.unit_link(user, eps)
    assert lk is not None and lk["ep"] == eps[-1]["id"] and "Killed process 424301 (systemd)" in lk["line"]
    assert oom.quiet_unit("user@1001.service") and oom.quiet_unit("user-runtime-dir@1001.service")
    fpm = {"unit": "php7.4-fpm.service", "result": "oom-kill", "since": now - 300, "log": []}
    assert "Killed process 477025 (python)" in oom.unit_link(fpm, eps)["line"]
    # упал сам по себе, OOM ни при чем
    assert oom.unit_link({"unit": "certbot.service", "result": "exit-code", "since": now - 300, "log": []}, eps) is None
    # helper'а нет: по строке в логе юнита все равно видно
    assert oom.unit_link(user, []) == {"ep": None, "line": ""}


async def test_units_alert_skips_oom_and_alert_text(tmp_path, monkeypatch):
    """Через collector: user@ и php-fpm, убитые OOM, не дают алерта "упал юнит", вместо них один
    алерт OOM; в "Что сломано" user@ нет, php-fpm - "убит OOM killer", есть пункт OOM."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'o.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    b = _block(now.timestamp())
    units = [
        {"unit": "user@1001.service", "type": "notify", "result": "signal", "status": 9, "since": now.timestamp() - 300,
         "restarts": 0, "log": ["user@1001.service: A process of this unit has been killed by the OOM killer."]},
        {"unit": "php7.4-fpm.service", "type": "notify", "result": "oom-kill", "status": 0,
         "since": now.timestamp() - 300, "restarts": 0, "log": []},
    ]
    rep = {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000,
           "extras": {"oom": b, "units": {"v": 1, "ts": now.timestamp() - 30, "units": units, "fix": True}}}
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=rep, oom_total=3,
                     alert_state={"oom_end": 0.0}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1, sent
    # последний эпизод: php7.4-fpm лежит - критично
    full = full_text(sent[0])
    assert sent[0].startswith("🔴🧠 ") and "нехватка памяти (OOM)" in full and "упал" not in full.split("\n")[0]
    assert "php7.4-fpm.service (не поднялась)" in full and "за 7 дней OOM 2 раз" in full
    # в чате: кого убило и что не поднялось, без "больше всего памяти" и счетчика за неделю
    assert sent[0] == "🔴🧠 node\n<b>Кончилась память (OOM)</b>\nУбит: python (пул web)\nНе поднялась: php7.4-fpm"
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        probs = collector.server_problems(srv, now)
    texts = [p["text"] for p in probs]
    assert not any("user@1001" in t for t in texts)
    assert "php7.4-fpm.service: убит OOM killer" in texts
    # за сутки один эпизод (второй неделю назад)
    assert any(p["kind"] == "oom" and p["level"] == 2 and p["text"].startswith(
        "нехватка памяти (OOM) из-за php7.4-fpm.service: была 5 мин назад, убиты python 554мб (php7.4-fpm.service, uid web)")
        for p in probs)
    view = oom.view(srv, now)
    assert view["n7"] == 2 and view["units"]["user@1001.service"]["quiet"] is True
    await engine.dispose()


async def test_report_stores_kills_and_api(client, auth_headers):
    """Отчет с блоком helper'а пишет убийства один раз; эпизоды отдаются карточке; счетчик ядра
    считается от прошлого отчета."""
    from app.models import OomKill, Server

    factory = client._transport.app.state.session_factory  # noqa: SLF001

    r = await client.post("/api/servers", json={"name": "web-a"}, headers=auth_headers)
    sid, token = r.json()["server"]["id"], r.json()["token"]
    now = datetime.now(timezone.utc).timestamp()
    b = _block(now)
    base = {"hostname": "web-a", "uptime_seconds": 5000, "clock_unix": int(now), "extras": {"oom": b}}
    h = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/agent/report", json={**base, "oom_total": 60}, headers=h)).status_code == 200
    assert (await client.post("/api/agent/report", json={**base, "oom_total": 63, "oom_kill": 0}, headers=h)).status_code == 200
    async with factory() as s:
        rows = (await s.execute(select(OomKill).where(OomKill.server_id == sid))).scalars().all()
        srv = await s.get(Server, sid)
        assert srv.oom_total == 3
    assert len([x for x in rows if x.kind != "unit"]) == 5 and len(rows) == 5 + len(b["units"])
    r = await client.get(f"/api/servers/{sid}/oom", headers=auth_headers)
    eps = r.json()["episodes"]
    assert [len(e["kills"]) for e in eps] == [3, 2] and eps[0]["kills"][-1]["owner"] == "php7.4-fpm.service"
    assert eps[1]["level"] == 2 and eps[0]["mem"]["ram"] == 8388068


def test_unit_link_by_cgroup_and_pending():
    """Случай с прода 20.10: systemd отметил OOM у user-1001.slice (helper слайсы не берет), а не у
    user@1001.service, в логе юнита строки про OOM нет. Убиты systemd + sd-pam из
    user@1001.service/init.scope - юнит все равно привязан к эпизоду; session-N.scope - так же."""
    now = 1_900_000_000.0
    b = _block(now)
    b["units"] = [u for u in b["units"] if not u["unit"].startswith("user@")]
    eps = oom.episodes(oom.records(b))
    user = {"unit": "user@1001.service", "result": "signal", "status": 9, "since": now - 300,
            "log": ["Startup finished in 144ms."]}
    lk = oom.unit_link(user, eps)
    assert lk is not None and lk["ep"] == eps[-1]["id"] and "Killed process 424301 (systemd)" in lk["line"]
    assert oom.cg_units("/user.slice/user-1001.slice/user@1001.service/init.scope") == {"user@1001.service", "init.scope"}
    sess = copy.deepcopy(b)
    for k in sess["kills"]:
        if "user@1001" in (k.get("memcg") or ""):
            k["memcg"] = "/user.slice/user-1001.slice/session-7.scope"
    eps2 = oom.episodes(oom.records(sess))
    assert oom.unit_link({"unit": "session-7.scope", "result": "signal", "status": 9, "since": now - 300}, eps2) is not None
    assert oom.unit_link(user, eps2) is None
    # убит KILL, helper еще не дочитал журнал за окно вокруг падения - ждем; дочитал - решено
    fresh = {"unit": "nginx.service", "result": "signal", "status": 9, "since": now - 130}
    assert oom.unit_pending(fresh, {"ts": now - 20}, now)
    assert not oom.unit_pending(fresh, {"ts": fresh["since"] + oom.UNIT_WINDOW}, now)
    assert not oom.unit_pending(fresh, None, now)  # helper'а нет - ждать нечего
    assert not oom.unit_pending(dict(fresh, status=15), {"ts": now - 20}, now)
    assert not oom.unit_pending(dict(fresh, since=now - 700), {"ts": now - 20}, now)


async def test_units_alert_user_manager_by_cgroup(tmp_path, monkeypatch):
    """Через collector, как на проде 20.10: отметки OOM у user@1001.service нет, лог юнита - чужие
    строки менеджера сессии. Одно сообщение OOM, без "упал user@1001.service". Пока helper не
    дочитал журнал за окно вокруг падения, "упал юнит" тоже не уходит."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'o.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    b = _block(now.timestamp())
    b["units"] = [u for u in b["units"] if not u["unit"].startswith("user@")]
    user = {"unit": "user@1001.service", "type": "notify-reload", "result": "signal", "status": 9,
            "since": now.timestamp() - 300, "restarts": 0, "log": [
                "Reached target default.target - Main User Target.", "Startup finished in 144ms.",
                "launchpadlib-cache-clean.service - Clean up old files in the Launchpadlib cache was skipped "
                "because of an unmet condition check (ConditionPathExists=/home/idv/.launchpadlib/api.launchpad.net/cache)."]}
    # убит KILL только что, журнал ядра helper прочитал раньше падения: OOM или нет, еще не ясно
    nginx = {"unit": "nginx.service", "type": "forking", "result": "signal", "status": 9,
             "since": now.timestamp() - 150, "restarts": 0, "log": []}
    rep = {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000,
           "extras": {"oom": dict(b, ts=now.timestamp() - 160),
                      "units": {"v": 1, "ts": now.timestamp() - 30, "units": [user, nginx], "fix": True}}}
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=rep, oom_total=3,
                     alert_state={"oom_end": 0.0}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    full = full_text(sent[0])
    assert len(sent) == 1 and "нехватка памяти (OOM)" in full, sent
    assert "user@1001" not in full and "launchpadlib" not in full
    assert sent[0].split("\n")[1] == "<b>Кончилась память (OOM)</b>" and "user@1001" not in sent[0]
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        texts = [p["text"] for p in collector.server_problems(srv, now)]
    assert not any("user@1001" in t or "nginx.service" in t for t in texts), texts
    await engine.dispose()


def test_culprit_is_who_held_memory():
    """Виновник - кто занимал память, а не первый убитый: в первом эпизоде ядро сначала убило mysqld
    (самый крупный процесс), а память съели воркеры php-fpm; во втором память у python из той же
    службы. Оба эпизода - один виновник, и мьют по нему глушит оба. Лимит cgroup - сама cgroup."""
    now = 1_900_000_000.0
    first, last = oom.episodes(oom.records(_block(now)))
    assert first["kills"][0]["name"] == "mysqld"
    assert oom.culprit(first) == oom.culprit(last) == "svc:php7.4-fpm.service"
    memcg = next(e for e in oom.episodes(oom.records(_helper_json(JOURNAL + EXTRA, 1792492100))) if e["kind"] == "memcg")
    assert oom.culprit(memcg) == "cg:run-r1a2b3c.scope"
    # только сессия пользователя и без таблицы: по имени процесса
    e = {"kind": "global", "top": [], "kills": [{"kind": "global", "name": "java", "anon": 900000, "uid": 0,
                                                 "memcg": "/", "user": ""}]}
    assert oom.culprit(e) == "proc:java"


def _muted_server(rep: dict, mutes=(), snoozes=None, **kw):
    s = _server(rep, **kw)
    s.alert_mutes, s.alert_snoozes = list(mutes), snoozes or {}
    return s


def test_alert_step_mute_by_culprit():
    """Заглушенный виновник: эпизод учтен молча (после мьюта о нем не придет), OOM из-за другой
    службы приходит; служба после OOM лежит - приходит и с мьютом; "oom" глушит все."""
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    b = _block(now.timestamp())
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": b}}
    st = {"oom_end": 0.0}
    key = "oom:svc:php7.4-fpm.service"
    fire, _d, upd = oom.alert_step(_muted_server(rep, total=3), st, rep, now, timezone.utc, {key})
    assert fire is None and upd["oom_end"] == max(e["end"] for e in oom.episodes(oom.records(b)))
    # мьют кончился: учтенное не приходит
    st.update(upd)
    fire, _d, _u = oom.alert_step(_muted_server(rep, total=3), st, rep, now + timedelta(minutes=5), timezone.utc, set())
    assert fire is None
    # чужой виновник заглушен - алерт идет, и ссылка ведет на эпизод
    fire, done, _ = oom.alert_step(_muted_server(rep, total=3), {"oom_end": 0.0}, rep, now, timezone.utc,
                                   {"oom:svc:mysql.service"})
    assert fire is not None and done["oom_ep"] == oom.episodes(oom.records(b))[-1]["id"]
    # php7.4-fpm лежит после OOM: мьют виновника не глушит
    units = {"v": 1, "ts": now.timestamp() - 30, "units": [{"unit": "php7.4-fpm.service", "result": "oom-kill",
                                                            "since": now.timestamp() - 300}]}
    rep_down = {**rep, "extras": {"oom": b, "units": units}}
    fire, _d, _u = oom.alert_step(_muted_server(rep_down, total=3), {"oom_end": 0.0}, rep_down, now, timezone.utc, {key})
    assert fire is not None and fire[0] == 2
    fire, _d, upd = oom.alert_step(_muted_server(rep_down, total=3), {"oom_end": 0.0}, rep_down, now, timezone.utc,
                                   {"oom"})
    assert fire is None and upd["oom_end"] > 0
    # без helper'а виновника не знать: глушит только "oom", счетчик учтен
    bare = {"clock_unix": now.timestamp()}
    fire, _d, upd = oom.alert_step(_muted_server(bare, total=2), {"oom_seen": 0}, bare, now, timezone.utc, {"oom"})
    assert fire is None and upd["oom_seen"] == 2
    assert oom.alert_step(_muted_server(bare, total=2), {"oom_seen": 0}, bare, now, timezone.utc, {key})[0] is not None


def test_problem_mute_and_snooze():
    """В "Что сломано" заглушенного нет (насовсем и на время), у пункта ключ мьюта по виновнику."""
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    rep = {"clock_unix": now.timestamp(), "extras": {"oom": _block(now.timestamp())}}
    p = oom.problem(_muted_server(rep), now)
    assert p is not None and p[2] == "oom:svc:php7.4-fpm.service"
    assert oom.problem(_muted_server(rep, mutes=[p[2]]), now) is None
    soon = (now + timedelta(hours=1)).isoformat()
    assert oom.problem(_muted_server(rep, snoozes={p[2]: soon}), now) is None
    assert oom.problem(_muted_server(rep, snoozes={"oom": soon}), now) is None
    # снуз кончился - снова видно
    assert oom.problem(_muted_server(rep, snoozes={p[2]: (now - timedelta(minutes=1)).isoformat()}), now) is not None
    view = oom.episode_view(oom.episodes(oom.records(rep["extras"]["oom"]))[-1], mutes={p[2]})
    assert view["culprit"] == "svc:php7.4-fpm.service" and view["muted"] is True


async def test_oom_mute_api_and_alert_link(tmp_path, monkeypatch, client, auth_headers):
    """Снуз по виновнику принимается API, эпизоды отдаются с пометкой; ссылка алерта ведет на эпизод."""
    r = await client.post("/api/servers", json={"name": "web-b"}, headers=auth_headers)
    sid, token = r.json()["server"]["id"], r.json()["token"]
    now = datetime.now(timezone.utc).timestamp()
    base = {"hostname": "web-b", "uptime_seconds": 5000, "clock_unix": int(now), "extras": {"oom": _block(now)}}
    assert (await client.post("/api/agent/report", json={**base, "oom_total": 3},
                              headers={"Authorization": f"Bearer {token}"})).status_code == 200
    r = await client.post(f"/api/servers/{sid}/snooze-alert", json={"kind": "oom:svc:php7.4-fpm.service", "hours": 24},
                          headers=auth_headers)
    assert r.status_code == 200 and "oom:svc:php7.4-fpm.service" in r.json()["alert_snoozes"]
    eps = (await client.get(f"/api/servers/{sid}/oom", headers=auth_headers)).json()["episodes"]
    assert all(e["muted"] and e["culprit"] == "svc:php7.4-fpm.service" for e in eps)

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'l.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    nd = datetime.now(timezone.utc)
    rep = {"clock_unix": int(nd.timestamp()), "uptime_seconds": 100000, "extras": {"oom": _block(nd.timestamp())}}
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=nd,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=rep, oom_total=3,
                     alert_state={"oom_end": 0.0}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook", panel_url="https://kervax.test"), nd)
    ep = oom.episodes(oom.records(rep["extras"]["oom"]))[-1]["id"]
    assert len(sent) == 1 and f"sec=oom&amp;oom={ep}" in sent[0], sent
    async with factory() as s:
        assert "oom_ep" not in (await s.execute(select(Server))).scalars().one().alert_state
    await engine.dispose()


def _kill(ts: float, name: str, uid: int, anon: int, memcg: str, top: list | None = None) -> dict:
    return {"ts": ts, "kind": "global", "pid": int(ts) % 100000, "name": name, "uid": uid, "user": "web" if uid == 1001 else "db",
            "anon": anon, "memcg": memcg, "top": top or [], "mem": {"ram": 8388068, "swap_total": 0}}


_TOP = [{"name": "php-fpm7.4", "uid": 1001, "user": "web", "n": 30, "anon": 6250904},
        {"name": "mysqld", "uid": 108, "user": "db", "n": 1, "anon": 539688}]
_SESSION = "/user.slice/user-1001.slice/user@1001.service/init.scope"


def _fpm_block(now: float, starts: list[float], tail: float | None = None, top: list | None = _TOP) -> dict:
    """Как на проде 20.10: в эпизоде память у php-fpm x30, а убиты сессия и mysqld; php-fpm убит
    только в отдельном эпизоде через две минуты (tail)."""
    kills = []
    for t in starts:
        kills += [_kill(t, "systemd", 1001, 1920, _SESSION, top), _kill(t + 0.03, "(sd-pam)", 1001, 1468, _SESSION, top),
                  _kill(t + 0.05, "mysqld", 108, 539688, "/system.slice/mysql.service", top)]
    if tail is not None:
        kills.append(_kill(tail, "php-fpm7.4", 1001, 400640, "/system.slice/php7.4-fpm.service", top))
    return {"v": 1, "ts": now - 20, "kills": kills, "units": []}


def test_culprit_not_killed_in_its_episode():
    """Виновника не убили в его эпизоде (убиты mysqld и сессия): служба по убийству того же процесса в
    соседнем эпизоде, без него - процесс из таблицы, но не убитый mysqld. Без таблицы - крупнейший убитый."""
    now = 1_900_000_000.0
    eps = oom.episodes(oom.records(_fpm_block(now, [now - 3600], tail=now - 3600 + 128)))
    assert len(eps) == 2 and eps[0]["kills"][-1]["name"] == "mysqld"
    assert oom.culprit(eps[0]) == "svc:php7.4-fpm.service"
    alone = oom.episodes(oom.records(_fpm_block(now, [now - 3600])))[0]
    assert oom.culprit(alone) == "proc:php-fpm7.4"
    blind = oom.episodes(oom.records(_fpm_block(now, [now - 3600], top=[])))[0]
    assert oom.culprit(blind) == "svc:mysql.service"
    assert oom.culprit_text("svc:php7.4-fpm.service") == "php7.4-fpm.service"
    assert oom.culprit_text("proc:java") == "процесс java" and oom.culprit_text("ct:web") == "контейнер web"
    # после "из-за" - родительный падеж; служба как есть
    assert oom.culprit_text("proc:java", gen=True) == "процесса java"
    assert oom.culprit_text("ct:web", gen=True) == "контейнера web"
    assert oom.culprit_text("svc:php7.4-fpm.service", gen=True) == "php7.4-fpm.service"
    key = {"oom:svc:php7.4-fpm.service"}
    assert oom.episode_muted(eps[0], key, set()) and not oom.episode_muted(alone, key, set())
    # кто занимал память, не знаем, а OOM сервера уже глушат по виновнику - считается заглушенным
    assert oom.episode_muted(blind, key, set()) and not oom.episode_muted(blind, set(), set())


def test_problem_home_lifetime_and_culprit_text():
    """Единичный OOM, после которого все поднялось, на главной час; повторы за сутки - пока они есть;
    служба лежит после OOM - пока лежит. В тексте виновник, его же глушит ключ строки, мьют убирает строку."""
    now = datetime.fromtimestamp(1_900_000_000, timezone.utc)
    ts = now.timestamp()

    def prob(b, **kw):
        return oom.problem(_muted_server({"clock_unix": ts, "extras": {"oom": b}}, **kw), now)

    # php-fpm убит три дня назад: его служба известна, хотя в эпизоде за сутки его не убивали
    p = prob(_fpm_block(ts, [ts - 1800], tail=ts - 3 * 86400))
    assert p == (2, "нехватка памяти (OOM) из-за php7.4-fpm.service: была 30 мин назад, убиты mysqld 527мб "
                    "(mysql.service, uid db), systemd + sd-pam (сессия web)", "oom:svc:php7.4-fpm.service")
    p = prob(_fpm_block(ts, [ts - 1800]))
    assert p[1] == ("нехватка памяти (OOM) из-за процесса php-fpm7.4: была 30 мин назад, убиты mysqld 527мб "
                    "(mysql.service, uid db), systemd + sd-pam (сессия web)") and p[2] == "oom:proc:php-fpm7.4"
    # 17 часов назад, один раз - событие прошло, на главной его нет
    assert prob(_fpm_block(ts, [ts - 17 * 3600])) is None
    # повторялся за сутки - видно, пока повторы в пределах суток
    p = prob(_fpm_block(ts, [ts - 17 * 3600, ts - 5 * 3600]))
    assert p is not None and "2 раз за сутки, последний 5 ч назад" in p[1]
    # мьют виновника на время убирает строку сразу
    soon = (now + timedelta(days=7)).isoformat()
    assert prob(_fpm_block(ts, [ts - 17 * 3600, ts - 5 * 3600], tail=ts - 5 * 3600 + 128),
                snoozes={"oom:svc:php7.4-fpm.service": soon}) is None
    # служба после OOM лежит - висит и через сутки без часа, и с мьютом виновника
    units = {"v": 1, "ts": ts - 30, "units": [{"unit": "mysql.service", "result": "oom-kill", "since": ts - 17 * 3600}]}
    rep = {"clock_unix": ts, "extras": {"oom": dict(_fpm_block(ts, [ts - 17 * 3600]),
                                                    units=[{"ts": ts - 17 * 3600, "unit": "mysql.service", "what": "failed"}]),
                                        "units": units}}
    p = oom.problem(_muted_server(rep), now)
    assert p is not None and p[0] == 2
