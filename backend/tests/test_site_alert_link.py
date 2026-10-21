"""Сайтовый алерт в чате: имя ведет в монитор панели, рядом 🔗 - на сам проверяемый сайт.

У не-HTTP мониторов (tcp, ping) открывать в браузере нечего - значка нет; у срока домена
текст про само имя домена, а не про адрес монитора - тоже без значка."""

from datetime import datetime, timezone

from app import collector, tg_fmt
from app.alerts import full_text
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check

PANEL = "https://panel.example.com"


async def _send(tmp_path, monkeypatch, check: dict, pending: list, panel_url: str = PANEL) -> list[str]:
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'link.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        c = Check(interval_seconds=1, enabled=True, **check)
        s.add(c)
        await s.commit()
        cid = c.id
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", panel_url=panel_url)
    await collector._send_alerts(factory, settings, [p[:5] + (cid,) + p[5:] for p in pending],
                                 datetime.now(timezone.utc))
    await engine.dispose()
    return sent


def test_site_link_helper():
    assert tg_fmt.site_link("https://example.com/") == '<a href="https://example.com/">🔗</a>'
    assert tg_fmt.site_link("") == ""
    # кавычки и амперсанд в пути не ломают href
    assert tg_fmt.site_link('https://example.com/a&b"c') == '<a href="https://example.com/a&amp;b&quot;c">🔗</a>'


async def test_down_alert_links_monitor_and_site(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://www.example.com/"},
                       [("bad", "web-a", "down", "HTTP 500", None, None)])
    assert len(sent) == 1
    first = str(sent[0]).split("\n")[0]
    assert first.startswith(f'🔴🌐 <a href="{PANEL}/?check=')
    assert first.endswith('>www.example.com</a> <a href="https://www.example.com/">🔗</a>')
    assert "\n<b>Не отвечает</b>\n" in sent[0]
    # в пачке одной строкой - с той же ссылкой на сайт
    assert '<a href="https://www.example.com/">🔗</a>: не отвечает' in sent[0].line


async def test_link_drops_query_and_escapes(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http",
                        "target": "https://example.com/a&b/health?token=SECRET1"},
                       [("bad", "web-a", "down", "HTTP 503", None, None)])
    text = str(sent[0])
    assert '<a href="https://example.com/a&amp;b/health">🔗</a>' in text
    assert "SECRET1" not in text and "SECRET1" not in full_text(sent[0])


async def test_recovery_has_site_link(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://example.com/"},
                       [("recovery", "web-a", "up", "HTTP 200", None, None)])
    assert str(sent[0]).endswith('</a> <a href="https://example.com/">🔗</a>: снова отвечает')


async def test_slow_has_site_link(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://example.com/"},
                       [("bad", "web-a", "degraded", "медленно: 4200 мс", None, None)])
    assert str(sent[0]).startswith("⚠️🌐 ")
    assert '<a href="https://example.com/">🔗</a>\n<b>Тормозит</b>' in sent[0]


async def test_ssl_alert_has_site_link(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://example.com/"},
                       [("ssl", "web-a", "", "сертификат истекает через 5 дн.", None, None)])
    assert len(sent) == 1
    assert '<a href="https://example.com/">🔗</a>\n' in sent[0]


async def test_tcp_monitor_without_site_link(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "db-a", "type": "tcp", "target": "example.com", "port": 5432},
                       [("bad", "db-a", "down", "connection refused", None, None)])
    assert len(sent) == 1
    assert "🔗" not in sent[0] and "🔗" not in sent[0].line
    assert f'<a href="{PANEL}/?check=' in sent[0]


async def test_domain_expiry_without_site_link(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://www.example.com/"},
                       [("domain", "web-a", "", "регистрация домена истекает через 5 дн.", None, None)])
    assert len(sent) == 1
    assert "🔗" not in sent[0]


async def test_without_panel_url_name_links_site_once(tmp_path, monkeypatch):
    """Панель без адреса: имя и так ведет на сайт, второй ссылки на то же не нужно."""
    sent = await _send(tmp_path, monkeypatch,
                       {"name": "web-a", "type": "http", "target": "https://example.com/"},
                       [("bad", "web-a", "down", "HTTP 500", None, None)], panel_url="")
    assert str(sent[0]).startswith('🔴🌐 <a href="https://example.com/">example.com</a>\n')
    assert "🔗" not in sent[0]
