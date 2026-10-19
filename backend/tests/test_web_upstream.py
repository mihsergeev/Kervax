"""Почему nginx отвечал 502/504 (helper webserver-setup 0.26): до какого upstream он не
достучался и как - в алерте по 5xx, в "Что сломано" и в разделе Веб."""
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import alert_what, collector, web_upstream
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerEvent, ServerMetric, WebErrorSample

SRC = (Path(__file__).resolve().parents[2] / "agent" / "webserver-setup.sh").read_text(encoding="utf-8")


def test_clean_keeps_only_what_is_understood():
    got = web_upstream.clean([
        {"u": "172.18.0.5:8000", "k": "refused", "n": 44, "c": "app-1"},
        {"u": "unix:/run/php/php8.3-fpm.sock", "k": "busy", "n": 3},
        {"u": "backend", "k": "whatever", "n": 2},          # неизвестный вид - "прочее"
        {"u": "x\"; rm -rf /", "k": "refused", "n": 1},        # кривой адрес - мимо
        {"u": "10.0.0.1:80", "k": "timeout", "n": 0},          # ноль - мимо
        {"u": "10.0.0.2:80", "k": "timeout", "n": 1, "c": "bad name!"},  # кривое имя - без имени
        "garbage",
    ])
    assert got == [
        {"u": "172.18.0.5:8000", "k": "refused", "n": 44, "c": "app-1"},
        {"u": "unix:/run/php/php8.3-fpm.sock", "k": "busy", "n": 3},
        {"u": "backend", "k": "other", "n": 2},
        {"u": "10.0.0.2:80", "k": "timeout", "n": 1},
    ]
    assert web_upstream.clean([]) is None and web_upstream.clean(None) is None
    assert len(web_upstream.clean([{"u": f"10.0.0.{i}:80", "k": "reset", "n": 1} for i in range(20)])) == 8


def test_text_names_container_with_port():
    acc: dict = {}
    web_upstream.add(acc, [{"u": "172.18.0.5:8000", "k": "refused", "n": 30, "c": "app-1"}])
    web_upstream.add(acc, [{"u": "172.18.0.5:8000", "k": "refused", "n": 14, "c": "app-1"},
                           {"u": "unix:/run/php/php8.3-fpm.sock", "k": "timeout", "n": 3},
                           {"u": "10.244.1.5:8080", "k": "refused", "n": 1, "c": "default-backend-80"}])
    assert web_upstream.text(acc) == ("app-1:8000 не принимает соединения - 44, "
                                      "unix:/run/php/php8.3-fpm.sock не ответил вовремя - 3")
    assert web_upstream.text(acc, 1, counts=False) == "app-1:8000 не принимает соединения"
    assert web_upstream.who({"u": "backend", "c": "x"}) == "x (backend)"
    assert web_upstream.text({}) == ""


def test_where_text_says_why():
    txt = collector.web_where_text({
        "logs": [["app-nginx (shop.example)", 44]], "codes": {"502": 44}, "paths": {"/api": 40}, "hosts": {},
        "ups": {("172.18.0.5:8000", "refused"): {"u": "172.18.0.5:8000", "k": "refused", "n": 44, "c": "app-1"}}})
    assert ". Коды: 502 - 44. Почему: app-1:8000 не принимает соединения - 44. Чаще всего падает: /api - 40" in txt
    # без причины - как раньше
    assert "Почему" not in collector.web_where_text({"logs": [["a", 1]], "codes": {"500": 1}, "paths": {}, "hosts": {}})


def test_report_line_takes_the_first_reason():
    body = ("3.10% запросов за 15 минут закончились ошибкой 5xx (44 из 1400). Больше всего: app-nginx - 44. "
            "Коды: 502 - 44. Почему: app-1:8000 не принимает соединения - 44, unix:/run/php/x.sock не ответил "
            "вовремя - 3. Чаще всего падает: /api - 40")
    assert alert_what._web5xx(body, []) == [
        ("ошибки 5xx на app-nginx", "3.10% запросов, чаще /api, app-1:8000 не принимает соединения")]
    one = "1.00% запросов. Больше всего: a - 1. Почему: 10.0.0.5:9000 не ответил вовремя - 7"
    assert alert_what._web5xx(one, [])[0][1] == "1.00% запросов, 10.0.0.5:9000 не ответил вовремя"


async def _panel(tmp_path, name):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_5xx_alert_says_why(tmp_path, monkeypatch):
    """Фронт панели во время выкладки: nginx отдавал 502, потому что backend еще не слушал порт.
    Алерт называет, до кого не достучались и как, - за этим не надо идти в error-лог."""
    engine, factory = await _panel(tmp_path, "why.db")
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report={"cpu_percent": 20}, cpu_alert_percent=0,
                     mem_alert_percent=0, disk_alert_percent=0, web_5xx_alert_percent=0.05))
        await s.commit()
        for m in range(15):
            ts = now - timedelta(minutes=m)
            s.add(ServerMetric(server_id=1, ts=ts, cpu_percent=20, web_rpm=1000, web_5xx=40))
            s.add(WebErrorSample(server_id=1, ts=ts, src_ts=1000 + m, e5=40, rpm=1000,
                                 log="docker:app-frontend", label="app-frontend", codes={"502": 40},
                                 ups=[{"u": "172.18.0.5:8000", "k": "refused", "n": 40, "c": "app-backend-1"}]))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1, sent
    assert "Почему: app-backend-1:8000 не принимает соединения - 600" in sent[0]
    await engine.dispose()


async def _deploy_case(tmp_path, name, events, ups_of=None):
    """Фронт панели 19.10.2026: две выкладки за восемь минут, пачки 502 к backend легли на три
    минуты helper'а. Остальные минуты окна - без ошибок."""
    engine, factory = await _panel(tmp_path, name)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report={"cpu_percent": 20}, cpu_alert_percent=0,
                     mem_alert_percent=0, disk_alert_percent=0, web_5xx_alert_percent=0.05))
        await s.commit()
        for m in range(15):
            s.add(ServerMetric(server_id=1, ts=now - timedelta(minutes=m), cpu_percent=20, web_rpm=1000,
                               web_5xx={9: 2, 2: 6, 1: 252}.get(m, 0)))
        default = {
            9: [{"u": "172.21.0.3:8000", "k": "refused", "n": 1}, {"u": "172.21.0.3:8000", "k": "reset", "n": 1}],
            2: [{"u": "backend", "k": "resolve", "n": 4}, {"u": "172.21.0.3:8000", "k": "reset", "n": 2}],
            1: [{"u": "backend", "k": "resolve", "n": 210},
                {"u": "172.21.0.3:8000", "k": "refused", "n": 43, "c": "app-backend-1"}],
        }
        for m, e5 in ((9, 2), (2, 6), (1, 252)):
            s.add(WebErrorSample(server_id=1, ts=now - timedelta(minutes=m), src_ts=1000 - m, e5=e5, rpm=1000,
                                 log="docker:app-frontend-1", label="app-frontend-1", codes={"502": e5},
                                 ups=(ups_of or default)[m]))
        for m, cat, kind, obj in events:
            s.add(ServerEvent(server_id=1, ts=now - timedelta(minutes=m), cat=cat, kind=kind, obj=obj,
                              text=f"{obj}: {kind}"))
        await s.commit()
    return engine, factory, now


async def _sent(factory, now, monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    return sent


DEPLOYS = [(10, "deploy", "image", "app-frontend-1"), (9, "deploy", "restart", "app-backend-1"),
           (2, "deploy", "restart", "app-backend-1")]


async def test_deploy_restart_is_not_a_5xx_alert(tmp_path, monkeypatch):
    """Отказы связи с контейнером, который в эти минуты выкатили, - секунды выкладки, не сбой."""
    engine, factory, now = await _deploy_case(tmp_path, "deploy.db", DEPLOYS)
    assert [x for x in await _sent(factory, now, monkeypatch) if "5xx" in x] == []
    async with factory() as s:
        w = (await collector.web_error_window(s, now))[1]
        assert w[4] == 260 and w[3] == {}
    await engine.dispose()


async def test_same_errors_without_deploy_still_alert(tmp_path, monkeypatch):
    """Без выкладки в хронологии те же 502 - алерт, как раньше. Упал сам (crash) - не выкладка."""
    engine, factory, now = await _deploy_case(tmp_path, "nodeploy.db",
                                              [(9, "crash", "crash", "app-backend-1"), (2, "crash", "crash", "app-backend-1")])
    sent = [x for x in await _sent(factory, now, monkeypatch) if "5xx" in x]
    assert len(sent) == 1 and "Почему: backend не резолвится - 214" in sent[0], sent
    await engine.dispose()


async def test_deploy_of_another_container_does_not_excuse(tmp_path, monkeypatch):
    """Выкатили один контейнер, а 502 - потому что лежит другой: это сбой."""
    ups = {m: [{"u": "172.21.0.9:9000", "k": "refused", "n": e5, "c": "app-worker-1"}]
           for m, e5 in ((9, 2), (2, 6), (1, 252))}
    engine, factory, now = await _deploy_case(tmp_path, "other.db", DEPLOYS, ups)
    assert len([x for x in await _sent(factory, now, monkeypatch) if "5xx" in x]) == 1
    await engine.dispose()


def test_restart_blip_rules():
    now = datetime.now(timezone.utc)
    dep = [(now - timedelta(minutes=1), "kervax-backend-1")]
    resolve = [{"u": "backend", "k": "resolve", "n": 9}]
    assert collector.restart_blip(resolve, 10, now, dep)
    # имя сервиса совпадает только целиком: backend - не kervax-backend-api-1
    assert not collector.restart_blip(resolve, 10, now, [(now, "kervax-backend-api-1")])
    # выкладка давно - минута считается
    assert not collector.restart_blip(resolve, 10, now, [(now - timedelta(minutes=5), "kervax-backend-1")])
    # медленный ответ - не выкладка, даже рядом с ней
    assert not collector.restart_blip([{"u": "backend:8000", "k": "timeout", "n": 9}], 10, now, dep)
    # отказов меньше 80% ошибок минуты: остальное - ответы самого приложения
    assert not collector.restart_blip(resolve, 20, now, dep)
    # внешний адрес без имени - не контейнер
    assert not collector.restart_blip([{"u": "203.0.113.5:443", "k": "refused", "n": 9}], 10, now, dep)
    assert collector.restart_blip([{"u": "192.168.16.4:8080", "k": "refused", "n": 9}], 10, now, dep)
    assert not collector.restart_blip(resolve, 10, now, None) and not collector.restart_blip(None, 10, now, dep)


def test_broken_summary_says_why():
    now = datetime.now(timezone.utc)
    block = {"ts": int(now.timestamp()), "rpm": 1000, "e5": 40, "logs": [
        {"log": "docker:app-frontend", "name": "app-frontend", "rpm": 1000, "e5": 40, "c5": {"502": 40},
         "up": [{"u": "172.18.0.5:8000", "k": "refused", "n": 40, "c": "app-backend-1"}]},
        {"log": "docker:muted-nginx", "name": "muted-nginx", "rpm": 10, "e5": 5,
         "up": [{"u": "10.0.0.9:80", "k": "timeout", "n": 5}]}]}
    s = Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
               offline_after_seconds=120, alert_sustain_seconds=900, alert_state={},
               last_report={"cpu_percent": 5, "extras": {"web-rate": block}, "clock_unix": now.timestamp()},
               alert_mutes=["web_5xx:muted-nginx"], alert_snoozes={}, cpu_alert_percent=0, mem_alert_percent=0,
               disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
               conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
               web_5xx_alert_percent=0.05, kube_expiry_warn_days=[])
    web_err = (15000.0, 600.0, 15, {"app-frontend": {1: 200.0, 2: 200.0, 3: 200.0}}, 0.0)
    probs = [p for p in collector.server_problems(s, now, web_err=web_err) if p.get("kind") == "web_5xx"]
    assert len(probs) == 1, probs
    assert probs[0]["text"].endswith("за 15 минут - app-backend-1:8000 не принимает соединения"), probs[0]
    assert "10.0.0.9" not in probs[0]["text"]  # заглушенный лог - не причина


async def test_errors_page_shows_why(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "web-b"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    log = {"log": "/var/log/nginx/access.log", "rpm": 100, "e5": 12, "c5": {"504": 12}, "sites": ["shop.example"],
           "up": [{"u": "unix:/run/php/php8.3-fpm.sock", "k": "timeout", "n": 12}, {"u": "bad\"", "k": "x", "n": 1}]}
    report = {"hostname": "web-b", "os": "U", "agent_version": "2.38", "cpu_percent": 5, "mem_used": 1,
              "mem_total": 2, "extras": {"web-rate": {"ts": int(time.time()), "rpm": 100, "e5": 12, "logs": [log]}}}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    rows = (await client.get(f"/api/servers/{sid}/web-errors?hours=1", headers=auth_headers)).json()
    assert rows[0]["ups"] == [{"u": "unix:/run/php/php8.3-fpm.sock", "k": "timeout", "n": 12}]


# --- helper: разбор строк error-лога nginx ---

def _rate_body() -> str:
    return re.search(r"<<'RATE_EOF'\n(.*?)\nRATE_EOF\n", SRC, re.S).group(1)


def test_helper_version_and_wiring():
    assert "KERVAX_SETUP_VERSION=0.26" in SRC and "\r" not in SRC
    body = _rate_body()
    # строки ошибок nginx в потоке контейнера: не в счет запросов, но причина берется
    assert "\\[(emerg|alert|crit|error|warn|notice|info|debug)\\]/ { up_take($0); next }" in body
    assert '"$UPFN"' in body and "up_out(el, kk) }" in body
    # error-лог хостового nginx - пятой колонкой карты, читается раз за запуск
    assert "errlog_chunk \"$ef\"" in body and 'uj=",\\"up\\":$up"' in body
    helper = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    assert "--error-log-path=" in helper and 'er[lg]=$5' in helper


def _run(script: str) -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    # файлом, а не через -c: на Windows длинная строка с кавычками и обратными слэшами
    # доходит до bash испорченной
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        out = subprocess.run([bash, path], capture_output=True, text=True, timeout=60, encoding="utf-8")
    finally:
        os.unlink(path)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _funcs() -> str:
    body = _rate_body()
    a = body.index("UPFN='")
    b = body.index("# Новый кусок каждого error-лога")
    c = body.index("# Адрес контейнера docker -> его имя")
    d = body.index('if [ -s "$MAP" ]; then')
    return "TAB=$(printf '\\t')\n" + body[a:b] + body[c:d]


J = '{"log":"%s\\n","stream":"stderr","time":"2026-10-19T10:00:00.1Z"}'
DOCKER = [
    J % '2026/10/19 10:00:00 [error] 20#20: *1 connect() failed (111: Connection refused) while connecting to upstream, client: 203.0.113.4, server: _, request: \\"GET /api HTTP/1.1\\", upstream: \\"http://172.18.0.5:8000/api\\", host: \\"panel.example\\"',
] * 3 + [
    J % '2026/10/19 10:00:01 [error] 20#20: *2 upstream timed out (110: Connection timed out) while reading response header from upstream, client: 203.0.113.4, server: _, upstream: \\"fastcgi://172.18.0.9:9000\\"',
    # вывод PHP и ушедший клиент - не отказ связи с upstream
    J % '2026/10/19 10:00:02 [error] 20#20: *3 FastCGI sent in stderr: \\"PHP message: oops\\" while reading response header from upstream, upstream: \\"fastcgi://172.18.0.9:9000\\"',
    J % '2026/10/19 10:00:03 [info] 20#20: *4 client prematurely closed connection while reading upstream, upstream: \\"http://172.18.0.5:8000/\\"',
    '{"log":"203.0.113.4 - - [19/Oct/2026:10:00:00 +0000] \\"GET /api HTTP/1.1\\" 502 150 \\"-\\" \\"Go\\"\\n","stream":"stdout","time":"2026-10-19T10:00:00.1Z"}',
]
HOST = [
    '2026/10/19 10:00:00 [error] 811#811: *5 connect() to unix:/run/php/php8.3-fpm.sock failed (11: Resource temporarily unavailable) while connecting to upstream, client: 203.0.113.4, server: a.example, upstream: "fastcgi://unix:/run/php/php8.3-fpm.sock:", host: "a.example"',
    '2026/10/19 10:00:00 [crit] 811#811: *6 connect() to unix:/run/php/php8.1-fpm.sock failed (2: No such file or directory) while connecting to upstream, client: 203.0.113.4, server: b.example, upstream: "fastcgi://unix:/run/php/php8.1-fpm.sock:", host: "b.example"',
    '2026/10/19 10:00:00 [error] 811#811: *7 no live upstreams while connecting to upstream, client: 203.0.113.4, server: a.example, upstream: "http://backend/api", host: "a.example"',
    '2026/10/19 10:00:00 [error] 811#811: *8 app.internal could not be resolved (3: Host not found), client: 203.0.113.4, server: c.example, host: "c.example"',
    '2026/10/19 10:00:00 [error] 811#811: *9 upstream prematurely closed connection while reading response header from upstream, client: 203.0.113.4, server: _, upstream: "http://127.0.0.1:3000/", host: "x"',
    '2026/10/19 10:00:00 [error] 811#811: *10 writev() failed (104: Connection reset by peer) while sending to client, client: 203.0.113.4, server: a.example, upstream: "http://127.0.0.1:3000/"',
    '2026/10/19 10:00:00 [error] 811#811: *11 open() "/var/www/x" failed (2: No such file or directory), client: 203.0.113.4, server: a.example, host: "a.example"',
]
ING = [
    '2026-10-19T10:00:00.1Z stdout F 203.0.113.4 - - [19/Oct/2026:10:00:00 +0000] "GET /api HTTP/2.0" 502 552 "-" "M" 71 0.001 [default-backend-80] [] 10.244.1.5:8080 0 0.001 502 abc',
    '2026-10-19T10:00:00.1Z stderr F 2026/10/19 10:00:00 [error] 31#31: *9 connect() failed (111: Connection refused) while connecting to upstream, client: 203.0.113.4, server: admin.example, upstream: "http://10.244.1.5:8080/api", host: "admin.example"',
]


def _lines(name: str, lines: list[str]) -> str:
    return f"cat > \"$T/{name}\" <<'L_EOF'\n" + "\n".join(lines) + "\nL_EOF\n"


def test_helper_reads_why_from_error_lines(tmp_path):
    t = tmp_path.as_posix()
    script = (f"set -u\nT='{t}'\n" + _funcs() + _lines("docker", DOCKER) + _lines("host", HOST) + _lines("ing", ING)
              + 'echo "D $(count_codes "" 60 1 < "$T/docker" | sed -n 6p)"\n'
              + 'echo "C $(count_codes "" 60 1 < "$T/docker" | sed -n 1p)"\n'
              + 'echo "I $(count_codes "" 60 1 < "$T/ing" | sed -n 6p)"\n'
              + 'echo "A $(up_errlog "a.example" 1 "a.example b.example" 60 1 < "$T/host")"\n'
              + 'echo "B $(up_errlog "b.example" 0 "a.example b.example" 60 1 < "$T/host")"\n'
              + 'echo "K $(up_errlog "b.example" 0 "a.example b.example" 120 3 < "$T/host")"\n'
              + "IPMAP='app-backend-1 172.18.0.5\nphp-fpm 172.18.0.9'\n"
              + 'echo "N $(up_names \'[{"u":"172.18.0.5:8000","k":"refused","n":3},{"u":"10.244.1.5:8080","k":"refused","n":1,"c":"x"}]\')"\n')
    out = dict(line.split(" ", 1) for line in _run(script).splitlines() if " " in line)
    # строки ошибок nginx не считаются запросами: 2 строки запросов, из них одна 502
    assert out["C"].split()[:2] == ["1", "1"]
    assert out["D"] == '[{"u":"172.18.0.5:8000","k":"refused","n":3},{"u":"172.18.0.9:9000","k":"timeout","n":1}]'
    # адрес пода -> upstream ingress-nginx из его access-строки с 502
    assert out["I"] == '[{"u":"10.244.1.5:8080","k":"refused","n":1,"c":"default-backend-80"}]'
    a = out["A"]
    # первый лог берет и свои строки, и строки сайтов, которых нет ни у кого (c.example, server _)
    for frag in ('{"u":"unix:/run/php/php8.3-fpm.sock","k":"busy","n":1}', '{"u":"backend","k":"nolive","n":1}',
                 '{"u":"app.internal","k":"resolve","n":1}', '{"u":"127.0.0.1:3000","k":"closed","n":1}'):
        assert frag in a, a
    assert "php8.1" not in a and "reset" not in a  # чужой сайт и "while sending to client"
    assert out["B"] == '[{"u":"unix:/run/php/php8.1-fpm.sock","k":"nofile","n":1}]'
    # в минуту: 1 строка за 120 с при выборке x3 -> 1.5, округляется до 2
    assert out["K"] == '[{"u":"unix:/run/php/php8.1-fpm.sock","k":"nofile","n":2}]'
    # имя контейнера по адресу; где имя уже есть (ingress) - не трогаем
    assert out["N"] == ('[{"c":"app-backend-1","u":"172.18.0.5:8000","k":"refused","n":3},'
                        '{"u":"10.244.1.5:8080","k":"refused","n":1,"c":"x"}]')


def test_helper_map_gets_error_log_column():
    helper = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    a = helper.index("extract_logs() {")
    b = helper.index("# Один лог - одна строка")
    conf = "\n".join([
        "error_log /var/log/nginx/error.log warn;",
        "http {",
        "  access_log /var/log/nginx/access.log;",
        "  server {",
        "    server_name a.example;",
        "  }",
        "  server {",
        "    server_name b.example;",
        "    access_log /var/log/nginx/b.access.log;",
        "    error_log /var/log/nginx/b.error.log;",
        "  }",
        "  server {",
        "    server_name c.example;",
        "    access_log /var/log/nginx/c.access.log;",
        "    error_log stderr;",
        "  }",
        "}",
    ])
    out = _run(helper[a:b] + f"\ncat <<'C_EOF' | extract_logs /var/log/nginx/default-error.log\n{conf}\nC_EOF\n")
    rows = [line.split("\t") for line in out.splitlines()]
    assert ["/var/log/nginx/access.log", "a.example", "", "", "/var/log/nginx/error.log"] in rows
    assert ["/var/log/nginx/b.access.log", "b.example", "", "", "/var/log/nginx/b.error.log"] in rows
    assert ["/var/log/nginx/c.access.log", "c.example", "", "", ""] in rows
    # без error_log вовсе - путь по умолчанию из nginx -V
    out = _run(helper[a:b] + "\nprintf 'http {\\n server {\\n  server_name d.example;\\n  access_log /x.log;\\n }\\n}\\n'"
               " | extract_logs /var/log/nginx/default-error.log\n")
    assert out.split("\n")[0].split("\t") == ["/x.log", "d.example", "", "", "/var/log/nginx/default-error.log"]
