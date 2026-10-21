"""Признаки взлома сайта (app/tamper.py): разбор страницы, база, подтверждение, уровни,
алерты и кнопки карточки."""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from app import collector, tamper
from app.config import Settings
from app.models import Check

NOW = datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc)
PAGE = ("<html><head><title>Example Shop</title><meta name='generator' content='WordPress 7.0'>"
        "<script src='/wp-content/themes/shop/js/custom.js'></script>"
        "<script src='https://www.googletagmanager.com/gtag/js?id=1'></script></head>"
        "<body>" + "x" * 13000 + "</body></html>")
WP = {"7.0": "insecure", "7.0.6": "insecure", "7.0.7": "outdated", "7.1.2": "insecure", "7.1.3": "latest"}
PHP = {"rel": {"8.3.36": ["21 Oct 2026", True], "8.3.35": ["10 Sep 2026", True], "8.3.34": ["01 Aug 2026", False]},
       "sup": ["8.2", "8.3", "8.4", "8.5"]}


def obs(size=13000, title="Example Shop", domains=None, bot=None, engine=None):
    return {"size": size, "title": title, "domains": domains or {"www.googletagmanager.com": "script"},
            "bot": bot, "engine": engine or {}}


def run(st, o, at, fleet=frozenset(), versions=None):
    return tamper.observe(st, o, at, set(fleet), versions or {})


def test_parse_page_domains_title_own_js():
    p = tamper.parse_page(PAGE + "<iframe src='//evil.example.net/check'></iframe>"
                          "<script>var u='https://203.0.113.9/x';</script>", "https://www.example.com/")
    assert p["title"] == "Example Shop"
    assert p["domains"] == {"www.googletagmanager.com": "script", "evil.example.net": "iframe",
                            "203.0.113.9": "inline"}
    assert p["js"] == ["https://www.example.com/wp-content/themes/shop/js/custom.js"]
    assert p["generator"] == "WordPress 7.0"


def test_js_domains_skip_docs_and_own():
    js = ('d.createElement("iframe").src="https://cdn-check.example.org/v";'
          'var ns="http://www.w3.org/2000/svg"; /* https://github.com/x */ fetch("/api")')
    assert tamper.js_domains(js) == {"cdn-check.example.org"}
    assert not tamper.is_external("static.example.com", "www.example.com")


def test_engine_from_generator_and_headers():
    assert tamper.engine({"x-powered-by": "PHP/8.3.31"}, "WordPress 7.0", "") == {"wp": "7.0", "php": "8.3.31"}
    assert tamper.engine({"server": "nginx"}, "", "<link href='/bitrix/cache/a.css'>") == {"gen": "1C-Bitrix"}


def test_wp_verdict_points_to_first_safe_in_branch():
    assert tamper.wp_verdict("7.0", WP) == {"what": "WordPress 7.0", "fix": "7.0.7"}
    assert tamper.wp_verdict("7.1.3", WP) is None
    assert tamper.wp_verdict("7.2-beta", WP) is None  # неизвестной версии не придумываем дыру


def test_php_verdict_security_fix_and_eol():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    # 8.3.36 вышел сегодня - не повод; 8.3.35 с исправлением полтора месяца назад - повод
    assert tamper.php_verdict("8.3.31", PHP, now) == {"what": "PHP 8.3.31", "fix": "8.3.35"}
    assert tamper.php_verdict("8.3.35", PHP, now) is None
    assert tamper.php_verdict("7.4.3", PHP, now) == {"what": "PHP 7.4.3", "eol": True}
    assert tamper.php_verdict("8.3.31", {}, now) is None  # версий не загружали - не проверяем


def test_empty_page_needs_two_scans_then_clears_itself():
    st = run(None, obs(), NOW)
    assert st["base"]["title"] == "Example Shop" and tamper.level(st) == "none"
    st = run(st, obs(size=173, title=""), NOW + timedelta(hours=1))
    assert tamper.level(st) == "none" and "empty" in st["pend"]
    assert st["next"] == (NOW + timedelta(hours=1) + tamper.RECHECK).isoformat()
    st = run(st, obs(size=173, title=""), NOW + timedelta(hours=1, minutes=10))
    assert tamper.level(st) == "hack"  # сильный признак - сам по себе повод
    assert st["signs"]["empty"]["since"] == (NOW + timedelta(hours=1)).isoformat()
    assert tamper.summary(st) == "похоже на взлом: главная пустая"
    # вылечили - признак уходит сам
    st = run(st, obs(), NOW + timedelta(hours=3))
    assert tamper.level(st) == "none"


def test_new_domain_is_weak_and_fleet_domain_is_not_new():
    st = run(None, obs(), NOW)
    bad = obs(domains={"www.googletagmanager.com": "script", "evil.example.net": "/js/custom.js",
                       "cdn.example.org": "script"})
    st = run(st, bad, NOW + timedelta(hours=1), fleet={"cdn.example.org"})
    st = run(st, bad, NOW + timedelta(hours=1, minutes=10), fleet={"cdn.example.org"})
    assert tamper.level(st) == "one"
    assert list(st["signs"]["dom"]["list"]) == ["evil.example.net"]
    assert tamper.summary(st) == "чужой домен evil.example.net"
    # плюс старый WordPress - уже два признака
    st = run(st, {**bad, "engine": {"wp": "7.0"}}, NOW + timedelta(hours=2), fleet={"cdn.example.org"},
             versions={"wp": WP})
    assert tamper.level(st) == "hack"
    text = tamper.alert_text(st)
    assert text.splitlines() == ["Похоже на взлом: 2 признака",
                                 "Чужой домен evil.example.net в custom.js",
                                 "WordPress 7.0 с дырой"]


def test_googlebot_sees_other_page():
    st = run(None, obs(), NOW)
    spam = obs(bot={"size": 41000, "title": "Spam Casino 2026"})
    st = run(st, spam, NOW + timedelta(hours=1))
    st = run(st, spam, NOW + timedelta(hours=1, minutes=10))
    assert tamper.level(st) == "hack" and "bot" in st["signs"]
    # такая же страница для бота и для людей - не признак, даже если цифры в title другие
    st2 = run(None, obs(title="News 21.10"), NOW)
    st2 = run(st2, obs(title="News 21.10", bot={"size": 13100, "title": "News 22.10"}), NOW + timedelta(hours=1))
    assert not st2["pend"] and not st2["signs"]


def test_accept_buttons():
    st = run(None, obs(), NOW)
    bad = obs(domains={"www.googletagmanager.com": "script", "evil.example.net": "iframe"},
              bot={"size": 41000, "title": "Other"}, engine={"wp": "7.0"})
    for m in (60, 70):
        st = run(st, bad, NOW + timedelta(minutes=m), versions={"wp": WP})
    assert set(st["signs"]) == {"dom", "bot", "old"}
    st = tamper.accept(st, "dom", "evil.example.net", NOW)
    assert "dom" not in st["signs"] and "evil.example.net" in st["base"]["domains"]
    st = tamper.accept(st, "old", "", NOW)
    assert "old" not in st["signs"]
    st = run(st, bad, NOW + timedelta(hours=2), versions={"wp": WP})
    assert set(st["signs"]) == {"bot"}  # отложенная версия и принятый домен не вернулись
    st = tamper.accept(st, "all", "", NOW)
    assert tamper.level(st) == "none"
    st = run(st, bad, NOW + timedelta(hours=3), versions={"wp": WP})
    assert not st["signs"] and not st["pend"]  # принятая разница для бота больше не признак


def test_alert_due_transitions():
    hack = {"signs": {"dom": {}, "title": {}}}
    assert tamper.alert_due(hack, None) == ("tamper", ["hack"])
    assert tamper.alert_due(hack, ["hack"]) is None
    worse = {"signs": {"dom": {}, "title": {}, "empty": {}}}
    assert tamper.alert_due(worse, ["hack"]) == ("tamper", ["empty", "hack"])
    assert tamper.alert_due({"signs": {"dom": {}}}, ["hack"]) == ("", [])
    assert tamper.alert_due({"signs": {}}, ["empty", "hack"]) == ("tamperrec", [])
    assert tamper.alert_due({"signs": {"dom": {}}}, None) is None


def test_texts_keyboard_only():
    st = {"signs": {"empty": {"size": 173, "was": 13000, "title": "Shop"},
                    "bot": {"title": "Spam"}, "dom": {"list": {"a.example.net": {"src": "iframe"}}},
                    "old": {"items": [{"what": "WordPress 7.0", "fix": "7.0.7"}]}}}
    for t in (tamper.summary(st), tamper.alert_text(st)):
        assert not re.search("[—–«»…ё≥→]", t), t
    assert tamper.alert_text(st).splitlines()[1] == 'Главная 173 байта вместо 13кб, Googlebot видит "Spam"'
    assert tamper.summary(st) == ("похоже на взлом: главная пустая, Googlebot видит другое, "
                                  "чужой домен a.example.net и еще 1")


async def test_scan_reads_page_js_and_googlebot(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        ua = req.headers["user-agent"]
        seen.append((req.url.path, "Googlebot" in ua))
        if req.url.path.endswith("custom.js"):
            return httpx.Response(200, text='x.src="https://evil.example.net/f";')
        if "Googlebot" in ua:
            return httpx.Response(200, text="<title>Spam</title>" + "y" * 40000,
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8",
                                                      "x-powered-by": "PHP/8.3.31"})

    real = httpx.AsyncClient
    monkeypatch.setattr(tamper.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    chk = SimpleNamespace(target="https://www.example.com/", auth_method="", http_headers="", ignore_tls=False)
    got = (await tamper.scan(chk))["obs"]
    assert got["title"] == "Example Shop" and got["size"] == len(PAGE)
    assert got["domains"]["evil.example.net"] == "/wp-content/themes/shop/js/custom.js"
    assert got["bot"]["title"] == "Spam"
    assert got["engine"] == {"wp": "7.0", "php": "8.3.31"}
    assert ("/", True) in seen


async def test_scan_skips_blocked_bot_and_non_html(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if "Googlebot" in req.headers["user-agent"]:
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(tamper.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    chk = SimpleNamespace(target="example.com/health", auth_method="", http_headers="", ignore_tls=False)
    assert await tamper.scan(chk) == {"err": "не html"}


async def test_alert_once_then_recovery(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    st = {"signs": {"empty": {"size": 173, "was": 13000, "title": "Shop", "since": NOW.isoformat()},
                    "dom": {"list": {"evil.example.net": {"src": "iframe", "since": NOW.isoformat()}}}}}
    async with f() as s:
        c = Check(name="web-a", type="http", target="https://www.example.com", tamper_state=st)
        s.add(c)
        await s.commit()
        cid = c.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    cfg = Settings(alert_webhook="http://hook", panel_url="https://panel.example.com")
    now = datetime.now(timezone.utc)
    await collector.tamper_alerts(f, cfg, now)
    assert len(sent) == 1
    text = str(sent[0])
    assert "🚨🕵️" in text and "Похоже на взлом: 2 признака" in text and "evil.example.net" in text
    assert "sec=tamper" in text
    await collector.tamper_alerts(f, cfg, now)
    assert len(sent) == 1  # уже сказали - молчим
    async with f() as s:
        (await s.get(Check, cid)).tamper_state = {"signs": {}}
        await s.commit()
    await collector.tamper_alerts(f, cfg, now)
    assert len(sent) == 2 and "признаков взлома больше нет" in str(sent[1])
    async with f() as s:
        assert (await s.get(Check, cid)).tamper_sent is None


async def test_api_accept_and_view(client, auth_headers):
    r = await client.post("/api/checks", json={"name": "web-a", "type": "http", "target": "https://www.example.com"},
                          headers=auth_headers)
    cid = r.json()["id"]
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        st = run(None, obs(), NOW)
        bad = obs(domains={"www.googletagmanager.com": "script", "evil.example.net": "iframe"})
        st = run(run(st, bad, NOW + timedelta(hours=1)), bad, NOW + timedelta(hours=2))
        (await s.get(Check, cid)).tamper_state = st
        await s.commit()
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    t = next(c for c in ov["checks"] if c["id"] == cid)["tamper"]
    assert t["level"] == "one" and t["summary"] == "чужой домен evil.example.net"
    assert t["signs"][0]["domain"] == "evil.example.net" and t["signs"][0]["src"] == "iframe"
    r = await client.post(f"/api/checks/{cid}/tamper-accept", json={"key": "dom", "arg": "evil.example.net"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["tamper"]["level"] == "none"
    r = await client.post(f"/api/checks/{cid}/tamper-accept", json={"key": "nope"}, headers=auth_headers)
    assert r.status_code == 400
    # другой адрес - база сбрасывается
    r = await client.patch(f"/api/checks/{cid}", json={"target": "https://shop.example.com"}, headers=auth_headers)
    assert r.json()["tamper"] is None
