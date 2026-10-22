"""Прогрев после включения (app/boot_warmup.py): сайты и сервисы сервера, который только что
загрузился или вернулся на связь, не алертят, пока все поднимается."""
from datetime import datetime, timedelta, timezone

from app import boot_warmup, collector, srv_short
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, Server

NOW = datetime(2026, 10, 22, 14, 26, tzinfo=timezone.utc)
_PENDING_POD = {"owner": "Deployment", "phase": "Pending", "reason": "ContainerCreating",
                "ns": "default", "name": "web-a-7d9f-x1"}


def test_window_counts_from_boot_or_from_return():
    # загрузился 2 минуты назад
    assert boot_warmup.warming({"uptime_seconds": 120}, NOW, NOW)
    assert not boot_warmup.warming({"uptime_seconds": 3600}, NOW, NOW)
    # аптайм большой, но агент только что вернулся после долгого молчания (сеть, пауза ВМ)
    back = (NOW - timedelta(minutes=3)).isoformat()
    assert boot_warmup.warming({"uptime_seconds": 86400, "back_at": back}, NOW, NOW)
    assert not boot_warmup.warming({"uptime_seconds": 86400, "back_at": (NOW - timedelta(minutes=11)).isoformat()},
                                   NOW, NOW)
    # окно вышло, а Kubernetes еще поднимает поды - ждем, но не дольше MAX
    rep = {"uptime_seconds": 900, "kube": {"pods": [_PENDING_POD]}}
    assert boot_warmup.warming(rep, NOW, NOW)
    assert not boot_warmup.warming(rep | {"uptime_seconds": 1300}, NOW, NOW)
    assert not boot_warmup.warming({"uptime_seconds": 900, "kube": {"pods": []}}, NOW, NOW)
    assert not boot_warmup.warming({}, NOW, NOW) and not boot_warmup.warming(None, None, NOW)


def test_came_back_ignores_the_panels_own_downtime():
    started = NOW - timedelta(hours=1)
    assert boot_warmup.came_back(NOW - timedelta(minutes=10), NOW, 300, started)
    assert not boot_warmup.came_back(NOW - timedelta(minutes=2), NOW, 300, started)
    # отчеты не доходили, потому что лежала панель: у всего парка разом, это не возврат сервера
    assert not boot_warmup.came_back(NOW - timedelta(minutes=10), NOW, 300, NOW - timedelta(minutes=1))
    assert not boot_warmup.came_back(None, NOW, 300, started)
    # отметка живет в отчетах, пока может держать прогрев
    assert boot_warmup.keep_back((NOW - timedelta(minutes=5)).isoformat(), NOW)
    assert boot_warmup.keep_back((NOW - timedelta(minutes=30)).isoformat(), NOW) is None
    assert boot_warmup.keep_back(None, NOW) is None


def _server(name, **kw):
    base = dict(name=name, token_hash="t-" + name, enabled=True, backup_not_required=True, last_seen=NOW,
                last_report={}, alert_state={}, cpu_alert_percent=0, mem_alert_percent=0,
                disk_alert_percent=0, offline_after_seconds=120, alert_sustain_seconds=0)
    base.update(kw)
    return Server(**base)


def _capture(monkeypatch):
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    return sent


async def test_site_checked_by_a_just_booted_server_waits(client, monkeypatch):
    """Живой случай: "снова на связи", через минуту "не отвечает" про сайты, которые агент
    проверяет изнутри, еще через минуту отбой. Поды просто поднимались."""
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = _server("web-a", last_seen=now, last_report={"uptime_seconds": 90})
        s.add(srv)
        await s.commit()
        a = Check(name="example.com", type="http", target="https://example.com", probe_local=True,
                  probe_server_id=srv.id)
        b = Check(name="other.example", type="http", target="https://other.example")
        s.add_all([a, b])
        await s.commit()
        cid, oid, sid = a.id, b.id, srv.id
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    msg = "изнутри сервера порт 443 закрыт — веб-сервер не слушает localhost"
    await collector._send_alerts(f, settings, [
        ("bad", "example.com", "down", msg, None, cid, None),
        ("bad", "other.example", "down", "HTTP 502", None, oid, None),
    ], now)
    assert [m.target for m in sent] == ["other.example"]
    # прогрев вышел, а сайт так и лежит - теперь один алерт
    async with f() as s:
        (await s.get(Server, sid)).last_report = {"uptime_seconds": 700}
        await s.commit()
    sent.clear()
    await collector._send_alerts(f, settings, [("bad", "example.com", "down", msg, None, cid, None)], now)
    assert [m.target for m in sent] == ["example.com"]
    assert str(sent[0]).split("\n")[1:] == ["<b>Не отвечает</b>", "изнутри сервера порт 443 закрыт"]


async def test_report_after_a_long_silence_starts_the_warmup(client, auth_headers):
    r = await client.post("/api/servers", headers=auth_headers, json={"name": "web-a"})
    assert r.status_code in (200, 201), r.text
    token, sid = r.json()["token"], r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"hostname": "web-a", "os": "Ubuntu", "agent_version": "2.44", "cpu_percent": 1.0, "mem_used": 1,
            "mem_total": 2, "disks": [], "uptime_seconds": 864000}
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = await s.get(Server, sid)
        assert "back_at" not in (srv.last_report or {})
        srv.last_seen = datetime.now(timezone.utc) - timedelta(minutes=20)
        await s.commit()
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    async with f() as s:
        back = (await s.get(Server, sid)).last_report.get("back_at")
    assert back
    # следующий отчет отметку несет дальше, нового возврата нет
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    async with f() as s:
        assert (await s.get(Server, sid)).last_report.get("back_at") == back


async def test_pods_of_a_just_booted_node_wait(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    rep = {"uptime_seconds": 120, "kube": {"access": True, "pods": [_PENDING_POD]}}
    async with factory() as s:
        s.add(_server("web-a", last_seen=now, last_report=rep))
        await s.commit()
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert not [m for m in sent if m.kind == "kube_pod"], [str(m) for m in sent]
    async with factory() as s:
        assert not (await s.get(Server, 1)).alert_state.get("kube_pod")
    # прогрев вышел (и MAX тоже), под так и не встал - алерт
    async with factory() as s:
        (await s.get(Server, 1)).last_report = rep | {"uptime_seconds": 1500}
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert [m.kind for m in sent if m.kind == "kube_pod"] == ["kube_pod"]
    await engine.dispose()


def test_agent_reasons_are_one_short_line():
    def facts(msg, status="down"):
        return srv_short.site("bad", status, msg)[2]

    assert facts("изнутри сервера порт 443 закрыт — веб-сервер не слушает localhost (так бывает у "
                 "Kubernetes), локальная проверка здесь не сработает, проверяйте из панели "
                 '(Get "https://example.com": dial tcp 127.0.0.1:443: c)') == ["изнутри сервера порт 443 закрыт"]
    assert facts("сервер оборвал запрос изнутри: похоже, белый список сайта — добавьте в него "
                 "127.0.0.1/32 (Get \"https://example.com\": EOF)") == [
        "сервер оборвал запрос изнутри: похоже, белый список сайта"]
    assert facts("сертификат истёк 08.09.2026 — выпустите новый (Get \"https://example.com\": tls: "
                 "failed to verify certificate: x509: certif)") == ["сертификат истёк 08.09.2026"]
    assert facts("агент не присылает результат 12 мин — проверка с сервера не идёт") == [
        "агент не присылает результат 12 мин"]
    # подсказка из словаря: без сырой ошибки, обрезанной или целой
    assert facts("сервер отклонил соединение — порт закрыт (dial tcp 10.0.0.5:443: connect: connection "
                 "refused)") == ["сервер отклонил соединение"]
    assert facts("истекло время ожидания (Get \"https://example.com\": context deadline exceeded "
                 "(Client.Timeout exceeded while awaiting headers))") == ["истекло время ожидания"]
    assert facts("не удалось подключиться ни по одному адресу (All connection attempts failed)") == [
        "не удалось подключиться ни по одному адресу"]
    assert facts('Get "https://example.com": stopped after 10 redirects') == ["stopped after 10 redirects"]
    # код ответа - это причина, а не сырая ошибка
    assert facts("плохой ответ от бэкенда (HTTP 502)") == ["плохой ответ от бэкенда (HTTP 502)"]
    assert facts("/api: доступ запрещён (HTTP 403)") == ["/api: доступ запрещён (HTTP 403)"]
    # через сервис кластера: шлюз молчит, приложение отвечает
    assert facts("сервер отклонил соединение — порт закрыт (dial tcp 10.0.0.5:80: connect: connection "
                 "refused) — сервис default/web:80 в обход шлюза") == [
        "сервер отклонил соединение", "сервис default/web:80 в обход шлюза"]
    # все адреса домена: сколько не отвечает и первый с причиной
    assert facts("1/2 адресов OK; 192.0.2.10 — сервер отклонил соединение — порт закрыт "
                 "(All connection attempts failed)") == [
        "не отвечает 1 из 2 адресов", "192.0.2.10: сервер отклонил соединение"]


async def test_container_restarts_while_booting_are_not_a_loop(tmp_path, monkeypatch):
    """Контейнер падал, пока не встала база: на прогреве это загрузка, не цикл перезапусков."""
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(rc):
        return {"uptime_seconds": 100, "docker": {"present": True, "access": True,
                "containers": [{"name": "web-a", "state": "running", "policy": "always", "restarts": rc}]}}

    async with factory() as s:
        s.add(_server("web-a", last_seen=now, last_report=rep(0)))
        await s.commit()
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    for rc in range(5):
        async with factory() as s:
            (await s.get(Server, 1)).last_report = rep(rc)
            await s.commit()
        await collector.evaluate_servers(factory, settings, now)
    assert not [m for m in sent if m.kind == "docker_loop"], [str(m) for m in sent]
    await engine.dispose()
