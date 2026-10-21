"""Сайтовый алерт в чате: имя ведет на сам проверяемый сайт, в конце строки с именем
" · монитор" ссылкой в монитор панели.

У не-HTTP мониторов (tcp, ping) открывать в браузере нечего - имя ведет в монитор, хвоста нет;
у срока домена текст про само имя домена, а не про адрес монитора - тоже без хвоста. Без адреса
панели имя ведет на сайт, хвоста нет."""

from datetime import datetime, timezone

from app import collector, tg_fmt
from app.alerts import full_text
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check

PANEL = "https://panel.example.com"


async def _send(tmp_path, monkeypatch, checks: list[dict], pending: list, panel_url: str = PANEL) -> list[str]:
    """pending - кортежи без check_id: id подставляется по номеру монитора в checks (последний
    элемент кортежа)."""
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'link.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ids = []
    async with factory() as s:
        for ch in checks:
            c = Check(interval_seconds=1, enabled=True, **ch)
            s.add(c)
            await s.flush()
            ids.append(c.id)
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", panel_url=panel_url)
    await collector._send_alerts(factory, settings, [p[:5] + (ids[p[5]], None) for p in pending],
                                 datetime.now(timezone.utc))
    await engine.dispose()
    return sent


WEB = {"name": "web-a", "type": "http", "target": "https://www.example.com/"}


def test_monitor_link_helper():
    assert tg_fmt.monitor_link(f"{PANEL}/?check=1") == f' · <a href="{PANEL}/?check=1">монитор</a>'
    assert tg_fmt.monitor_link("") == ""
    # амперсанд в адресе панели экранируется в href
    assert tg_fmt.monitor_link(f"{PANEL}/?check=1&sec=exp") == f' · <a href="{PANEL}/?check=1&amp;sec=exp">монитор</a>'


async def test_down_name_links_site_and_tail_monitor(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch, [WEB], [("bad", "web-a", "down", "HTTP 500", None, 0)])
    assert len(sent) == 1
    first, what = str(sent[0]).split("\n")[:2]
    assert first.startswith('🔴🌐 <a href="https://www.example.com/">www.example.com</a> · ')
    assert first.endswith(f'<a href="{PANEL}/?check=1">монитор</a>')
    assert what == "<b>Не отвечает</b>"
    assert "🔗" not in sent[0]
    # в пачке одной строкой: "монитор" в конце, после того, что случилось
    assert sent[0].line.startswith('🔴🌐 <a href="https://www.example.com/">www.example.com</a>: не отвечает')
    assert sent[0].line.endswith(f' · <a href="{PANEL}/?check=1">монитор</a>')


async def test_link_drops_query_and_escapes(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       [{"name": "web-a", "type": "http", "target": "https://example.com/a&b/health?token=SECRET1"}],
                       [("bad", "web-a", "down", "HTTP 503", None, 0)])
    text = str(sent[0])
    assert text.startswith('🔴🌐 <a href="https://example.com/a&amp;b/health">example.com/a&amp;b/health</a> · ')
    assert "SECRET1" not in text and "SECRET1" not in full_text(sent[0])


async def test_recovery_one_line_with_monitor_tail(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       [{"name": "web-a", "type": "http", "target": "https://example.com/"}],
                       [("recovery", "web-a", "up", "HTTP 200", None, 0)])
    assert str(sent[0]) == ('✅ <a href="https://example.com/">example.com</a>: снова отвечает'
                            f' · <a href="{PANEL}/?check=1">монитор</a>')


async def test_slow_tail_on_name_line(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch, [WEB], [("bad", "web-a", "degraded", "медленно: 4200 мс", None, 0)])
    assert str(sent[0]).startswith('⚠️🌐 <a href="https://www.example.com/">')
    assert f'<a href="{PANEL}/?check=1">монитор</a>\n<b>Тормозит</b>' in sent[0]


async def test_ssl_tail_leads_to_cert_section(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch, [WEB],
                       [("ssl", "web-a", "", "сертификат истекает через 5 дн.", None, 0)])
    assert len(sent) == 1
    first = str(sent[0]).split("\n")[0]
    assert '<a href="https://www.example.com/">www.example.com</a> · ' in first
    assert first.endswith(f'<a href="{PANEL}/?check=1&amp;sec=exp">монитор</a>')


async def test_mentions_kept(tmp_path, monkeypatch):
    web = dict(WEB, name="web-a @alice")
    sent = await _send(tmp_path, monkeypatch, [web],
                       [("bad", "web-a @alice", "down", "HTTP 500", None, 0),
                        ("recovery", "web-a @alice", "up", "HTTP 200", None, 0)])
    assert len(sent) == 2
    # падение: упоминание в конце, хвост "монитор" остается в строке с именем
    down = str(sent[0]).split("\n")
    assert down[0].endswith(">монитор</a>") and down[-1].endswith(" @alice")
    # отбой одной строкой: упоминание после "монитор"
    assert str(sent[1]).endswith(f'снова отвечает · <a href="{PANEL}/?check=1">монитор</a> @alice')


async def test_same_domain_fold_keeps_tail_on_first_line(tmp_path, monkeypatch):
    """www и голый домен легли вместе: одно сообщение, "Еще" отдельной строкой в конце, хвост
    "монитор" - в строке с именем."""
    sent = await _send(tmp_path, monkeypatch,
                       [WEB, {"name": "web-b", "type": "http", "target": "https://example.com/"}],
                       [("bad", "web-a", "down", "HTTP 500", None, 0),
                        ("bad", "web-b", "down", "HTTP 500", None, 1)])
    assert len(sent) == 1
    lines = str(sent[0]).split("\n")
    assert lines[0].endswith(f'<a href="{PANEL}/?check=1">монитор</a>')
    assert lines[-1] == "Еще: example.com"


async def test_tcp_monitor_name_links_monitor_without_tail(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       [{"name": "db-a", "type": "tcp", "target": "example.com", "port": 5432}],
                       [("bad", "db-a", "down", "connection refused", None, 0)])
    assert len(sent) == 1
    assert str(sent[0]).startswith(f'🔴🌐 <a href="{PANEL}/?check=1">example.com</a>\n')
    assert ">монитор</a>" not in sent[0] and ">монитор</a>" not in sent[0].line


async def test_domain_expiry_without_tail(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch, [WEB],
                       [("domain", "web-a", "", "регистрация домена истекает через 5 дн.", None, 0)])
    assert len(sent) == 1
    first = str(sent[0]).split("\n")[0]
    assert f'<a href="{PANEL}/?check=1' in first and ">example.com</a>" in first
    assert ">монитор</a>" not in sent[0]


async def test_without_panel_url_name_links_site_without_tail(tmp_path, monkeypatch):
    sent = await _send(tmp_path, monkeypatch,
                       [{"name": "web-a", "type": "http", "target": "https://example.com/"}],
                       [("bad", "web-a", "down", "HTTP 500", None, 0)], panel_url="")
    assert str(sent[0]).startswith('🔴🌐 <a href="https://example.com/">example.com</a>\n')
    assert "монитор" not in sent[0]
