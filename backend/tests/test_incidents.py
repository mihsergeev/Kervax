import httpx
import pytest
from sqlalchemy import func, select

from app import collector
from app.checks import CheckOutcome
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, CheckIncident


# --- поток инцидента: open → alert после N → recovery ---

async def test_incident_flow_and_alerts(tmp_path, monkeypatch):
    db = (tmp_path / "inc.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with factory() as s:
        c = Check(name="site", type="http", target="https://x", interval_seconds=1, enabled=True)
        s.add(c)
        await s.commit()
        cid = c.id

    seq = ["up", "down", "down", "down", "up"]
    box = {"i": 0}

    async def fake_run(check):
        st = seq[box["i"]]
        box["i"] += 1
        return CheckOutcome(st, message=st)

    monkeypatch.setattr("app.checks.run_check", fake_run)

    async def no_expiry(check):
        return None  # не ходим в сеть за сроками TLS/домена в тестах

    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)

    settings = Settings(alert_after_failures=2, alert_webhook="http://hook")

    async def force_due():
        async with factory() as s:
            row = await s.get(Check, cid)
            row.last_checked_at = None
            await s.commit()

    for _ in seq:
        await force_due()
        await collector.run_due_checks(factory, settings)

    # после «up→down×3→up»: инцидент закрыт, 2 алерта (down + recovery)
    async with factory() as s:
        incidents = list(await s.scalars(select(CheckIncident)))
        open_cnt = await s.scalar(
            select(func.count()).select_from(CheckIncident).where(
                CheckIncident.ended_at.is_(None)
            )
        )
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc.ended_at is not None and inc.notified is True
    assert open_cnt == 0
    assert len(sent) == 2
    # адрес — ссылка на сам сайт, иконка статуса (имя как таковое не показываем)
    assert "🔴" in sent[0] and 'href="https://x"' in sent[0] and "site" not in sent[0]
    assert "✅" in sent[1] and 'href="https://x"' in sent[1]
    await engine.dispose()


async def test_alert_gated_by_muted(tmp_path, monkeypatch):
    db = (tmp_path / "q.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(
            name="x", type="http", target="h", interval_seconds=1,
            enabled=True, alert_after_failures=1,
        ))
        await s.commit()

    async def fake_run(check):
        return CheckOutcome("down", message="bad")

    monkeypatch.setattr("app.checks.run_check", fake_run)

    async def no_expiry(check):
        return None

    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)

    # алерты временно потушены (muted) → алерт подавлен, но инцидент открывается
    async def muted_true(session):
        return True

    monkeypatch.setattr("app.settings_store.get_muted", muted_true)
    settings = Settings(alert_after_failures=1, alert_webhook="http://hook")
    await collector.run_due_checks(factory, settings)
    async with factory() as s:
        open_cnt = await s.scalar(
            select(func.count()).select_from(CheckIncident).where(
                CheckIncident.ended_at.is_(None)
            )
        )
    assert open_cnt == 1  # инцидент есть
    assert sent == []  # но алерт подавлен (muted)
    await engine.dispose()


async def test_site_alert_rules_disable_and_custom_text(tmp_path, monkeypatch):
    from app import settings_store

    db = (tmp_path / "srules.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(
            name="site", type="http", target="h", interval_seconds=1,
            enabled=True, alert_after_failures=1,
        ))
        await s.commit()

    async def fake_run(check):
        return CheckOutcome("down", message="502")

    async def no_expiry(check):
        return None

    monkeypatch.setattr("app.checks.run_check", fake_run)
    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_after_failures=1, alert_webhook="http://hook")

    # правило down выключено → инцидент открывается, но алерта нет
    async with factory() as s:
        await settings_store.set_site_alert_rules(s, {"down": {"enabled": False}})
    await collector.run_due_checks(factory, settings)
    assert sent == []

    # включаем с кастомным текстом → следующее падение (нового инцидента нет, но
    # проверим формат напрямую через _send_alerts)
    async with factory() as s:
        await settings_store.set_site_alert_rules(
            s, {"down": {"enabled": True, "text": "УПАЛ {name}: {message}"}}
        )
    pending = [("bad", "site", "down", "502", None, None, None)]
    await collector._send_alerts(factory, settings, pending, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert len(sent) == 1 and sent[0] == "УПАЛ site: 502"
    await engine.dispose()


# --- API: uptime + алерты ---

@pytest.fixture
def _fake_up(monkeypatch):
    async def fake(check):
        return CheckOutcome("up", latency_ms=10, message="ok")

    monkeypatch.setattr("app.checks.run_check", fake)


async def test_overview_uptime(client: httpx.AsyncClient, auth_headers, _fake_up):
    r = await client.post(
        "/api/checks", json={"name": "u", "type": "http", "target": "https://x"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    r = await client.get("/api/checks/overview", headers=auth_headers)
    ov = r.json()
    assert ov["open_incidents"] == 0
    assert ov["checks"][0]["uptime_24h"] == 100.0


async def test_history_uptime_incidents_endpoints(
    client: httpx.AsyncClient, auth_headers, _fake_up
):
    r = await client.post(
        "/api/checks", json={"name": "h", "type": "http", "target": "https://x"},
        headers=auth_headers,
    )
    cid = r.json()["id"]
    await client.post(f"/api/checks/{cid}/run", headers=auth_headers)

    r = await client.get(f"/api/checks/{cid}/history?hours=24", headers=auth_headers)
    assert r.status_code == 200 and len(r.json()["points"]) >= 1
    assert r.json()["points"][0]["status"] == "up"

    r = await client.get(f"/api/checks/{cid}/uptime", headers=auth_headers)
    assert r.status_code == 200 and r.json()["day"] == 100.0

    r = await client.get(f"/api/checks/incidents?check_id={cid}", headers=auth_headers)
    assert r.status_code == 200 and r.json() == []


async def test_check_log(client: httpx.AsyncClient, auth_headers, monkeypatch):
    r = await client.post(
        "/api/checks", json={"name": "lg", "type": "http", "target": "https://x"},
        headers=auth_headers,
    )
    cid = r.json()["id"]

    async def fake_down(check):
        return CheckOutcome("down", message="HTTP 503")

    monkeypatch.setattr("app.checks.run_check", fake_down)
    await client.post(f"/api/checks/{cid}/run", headers=auth_headers)

    r = await client.get(f"/api/checks/{cid}/log", headers=auth_headers)
    assert r.status_code == 200 and len(r.json()) >= 1

    r = await client.get(f"/api/checks/{cid}/log?failed=1", headers=auth_headers)
    assert r.status_code == 200
    assert any("HTTP 503" in x["message"] for x in r.json())  # сообщение об ошибке в журнале


async def test_alerts_config_api(client: httpx.AsyncClient, auth_headers):
    r = await client.get("/api/alerts", headers=auth_headers)
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = await client.put(
        "/api/alerts",
        json={"webhook": "http://hook.example", "muted": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["webhook"] == "http://hook.example"
    assert body["muted"] is True

    # test без реального канала (webhook недоступен) — sent может быть False с ошибкой
    r = await client.post("/api/alerts/test", headers=auth_headers)
    assert r.status_code == 200 and "sent" in r.json()


async def test_alert_rich_default_format(tmp_path, monkeypatch):
    """Дефолтный алерт: «<иконка> <адрес-ссылка> — <текст> · монитор». Имя не
    показываем, но @упоминания из него добавляем (для тегов). Без брендинга."""
    db = (tmp_path / "fmt.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        c = Check(name="Бэкенд @alice @bob", type="http",
                  target="https://api.example.com/health", interval_seconds=1, enabled=True)
        s.add(c)
        await s.commit()
        cid = c.id

    sent: list[tuple] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append((text, parse_mode))
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", panel_url="https://panel.example.com")

    pending = [("bad", "Бэкенд @alice @bob", "down",
                "timeout of 48000ms exceeded", None, cid, None)]
    from datetime import datetime, timezone
    await collector._send_alerts(factory, settings, pending, datetime.now(timezone.utc))
    assert len(sent) == 1
    text, pm = sent[0]
    assert pm == "HTML"
    # адрес — ссылка на сам сайт; отдельная ссылка «монитор» — на панель
    assert 'href="https://api.example.com/health"' in text  # открыть сайт
    assert '<a href="https://panel.example.com/?check=' in text and ">монитор</a>" in text
    assert "🔴" in text and "timeout of 48000ms exceeded" in text
    # имя-текст («Бэкенд») не показываем, но @упоминания сохранены для тегов
    assert "Бэкенд" not in text
    assert "@alice" in text and "@bob" in text
    assert "Kervax" not in text and "«" not in text
    await engine.dispose()


async def test_alert_strips_token_and_dedupes_name(tmp_path, monkeypatch):
    """Адрес в алерте — без query (токены/пароли не светятся), а если имя монитора =
    домену, оно не дублируется: адрес сам становится ссылкой (короче/читабельнее)."""
    db = (tmp_path / "fmt2.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        # имя == домену, а в URL — секретный токен в query
        s.add(Check(name="shop.example.com", type="http",
                    target="https://shop.example.com/registration/get-active?access-token=SECRET123",
                    interval_seconds=1, enabled=True))
        await s.commit()
        cid = (await s.scalar(select(Check))).id

    sent: list = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", panel_url="https://panel.example.com")
    from datetime import datetime, timezone
    pending = [("recovery", "shop.example.com", "up", "HTTP 200 · 211 мс", None, cid, None)]
    await collector._send_alerts(factory, settings, pending, datetime.now(timezone.utc))

    text = sent[0]
    # токен нигде: ни в тексте, ни в href ссылки на сайт (query отброшен)
    assert "SECRET123" not in text and "access-token" not in text
    # ссылка «открыть сайт» — на адрес без query
    assert 'href="https://shop.example.com/registration/get-active"' in text
    # ссылка «монитор» — на панель
    assert '<a href="https://panel.example.com/?check=' in text and ">монитор</a>" in text
    # имя == домен → отдельным текстом НЕ дублируется (в тексте нет « — acs… — »)
    assert " — shop.example.com —" not in text
    assert text.startswith("✅ ") and "HTTP 200 · 211 мс" in text
    await engine.dispose()


async def test_snooze_suppresses_site_alert(tmp_path, monkeypatch):
    """Снуз монитора (snooze_until в будущем) → алерт не шлётся; после истечения —
    шлётся снова (состояние не помечалось)."""
    from datetime import datetime, timedelta, timezone

    db = (tmp_path / "snz.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Check(name="site", type="http", target="https://x", interval_seconds=1,
                    enabled=True, snooze_until=now + timedelta(hours=1)))
        await s.commit()
        cid = (await s.scalar(select(Check))).id

    sent: list = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    pending = [("bad", "site", "down", "502", None, cid, None)]

    # снуз активен → тишина
    await collector._send_alerts(factory, settings, pending, now)
    assert sent == []

    # снуз истёк → алерт уходит
    async with factory() as s:
        (await s.scalar(select(Check))).snooze_until = now - timedelta(minutes=1)
        await s.commit()
    await collector._send_alerts(factory, settings, pending, now)
    assert len(sent) == 1 and "🔴" in sent[0]
    await engine.dispose()


async def test_domain_alert_collapses_by_registrable_domain(tmp_path, monkeypatch):
    """Пять мониторов на поддоменах одного домена → ОДИН алерт про сам домен.

    Домен продлевают целиком: истекает одно имя, а не пять. До свёртки в чат
    улетало пять одинаковых «регистрация домена истекает через 4 дн.», и список
    выглядел как пять разных проблем."""
    from datetime import datetime, timezone

    db = (tmp_path / "dom.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    subs = ["gitlab", "y", "mmbot", "msg", "env"]
    ids: list[int] = []
    async with factory() as s:
        for sub in subs:
            c = Check(name=f"{sub}.example.com", type="http",
                      target=f"https://{sub}.example.com", interval_seconds=60, enabled=True)
            s.add(c)
            await s.commit()
            ids.append(c.id)
        # монитор на ЧУЖОМ домене — он обязан остаться отдельной строкой
        other = Check(name="example.net", type="http",
                      target="https://go.example.net", interval_seconds=60, enabled=True)
        s.add(other)
        await s.commit()
        other_id = other.id

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", panel_url="https://panel.example.com")

    pending = [
        ("domain", f"{sub}.example.com", "", "регистрация домена истекает через 4 дн.",
         None, cid, ("domain_alerted_days", 7), "⚠️🌐")
        for sub, cid in zip(subs, ids)
    ] + [
        ("domain", "example.net", "", "регистрация домена истекает через 12 дн.",
         None, other_id, ("domain_alerted_days", 14), "⚠️🌐")
    ]
    await collector._send_alerts(factory, settings, pending, datetime.now(timezone.utc))

    # два сообщения: одно про example.com (с пометкой про пять мониторов), одно про чужой
    assert len(sent) == 2, sent
    plg = next(t for t in sent if "example.com" in t)
    assert "мониторов: 5" in plg
    # в шапке само истекающее имя, а не поддомен одного из мониторов
    assert "gitlab.example.com" not in plg
    # второе сообщение — про ЧУЖОЙ домен; искать его по «нет example.com» нельзя:
    # ссылка на панель (panel.example.com) есть в обоих
    other = next(t for t in sent if t is not plg)
    assert "example.net" in other

    # флаг эскалации должен быть выставлен КАЖДОМУ монитору, иначе на следующем
    # тике четыре оставшихся напишут по второму разу
    async with factory() as s:
        for cid in ids:
            row = await s.get(Check, cid)
            assert row.domain_alerted_days == 7, cid
