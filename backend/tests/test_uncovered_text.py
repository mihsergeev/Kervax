"""Coverage notifications stay short; commands and complete lists stay in the panel."""
import html
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import alerts, collector, settings_store
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server

BASE = "https://kervax.example.com"


def _srv(sid: int, name: str) -> Server:
    return Server(id=sid, name=name, token_hash="x")


def _plain(text):
    return html.unescape(re.sub(r"<[^>]*>", "", text))


def test_one_server_two_gaps_three_lines_with_detail_links():
    text = collector._uncov_short([(_srv(15, "bk-01"), ["kube", "helper:kubeexpiry-setup"])], BASE)
    lines = text.splitlines()
    assert len(lines) == 3
    assert _plain(lines[0]) == "🧩 Покрытие: 1 сервер"
    assert lines[1] == "Kubernetes без доступа, нет kubeexpiry-setup"
    assert lines[2] == f'<a href="{BASE}/?kube=15">bk-01</a>'
    assert f'href="{BASE}/?section=servers"' in lines[0]
    assert "ansible-playbook" not in text and "Что сделать" not in text


@pytest.mark.parametrize("count", [12, 41, 1000])
def test_fleet_size_does_not_expand_the_notification(count):
    rows = [(_srv(i, f"node-{i:04}"), ["helper:webfiles-setup"]) for i in range(1, count + 1)]
    text = collector._uncov_short(rows, BASE)
    assert len(text.splitlines()) == 3
    assert len(_plain(text)) < 200
    assert text.count('?server=') == 3
    assert f"и еще {count - 3}" in text
    assert "node-0004" not in text and "ansible" not in text


def test_docker_link_leads_to_access_setup_in_panel():
    text = collector._uncov_short([(_srv(7, "web-a"), ["docker"])], BASE)
    assert text.splitlines()[1] == "Docker без доступа"
    assert f'<a href="{BASE}/?docker=7">web-a</a>' in text
    assert "proxy" not in text


def test_mixed_gaps_and_names_have_one_shared_limit():
    rows = [(_srv(1, "k8s-a"), ["kube"]), (_srv(2, "db-a"), ["helper:dbstat-setup"]),
            (_srv(3, "db-b"), ["helper:dbstat-setup"]), (_srv(4, "web-a"), ["docker"]),
            (_srv(5, "web-b"), ["helper:webserver-setup"])]
    text = collector._uncov_short(rows, BASE)
    assert len(text.splitlines()) == 3
    assert text.splitlines()[1] == "Kubernetes без доступа, нет dbstat-setup, Docker без доступа и еще 1"
    assert text.count('href=') == 4
    assert "web-a" not in text and "web-b" not in text
    assert text.endswith("и еще 2")


def test_dynamic_values_are_flat_escaped_and_bounded():
    rows = [(_srv(i, "<node>\n" + "x" * 200), ["helper:" + "h" * 300]) for i in range(1, 9)]
    text = collector._uncov_short(rows, BASE + '/?x="&y=1')
    assert len(text.splitlines()) == 3
    assert len(_plain(text)) < 350
    assert "<node>" not in text and "&lt;node&gt;" in text
    assert 'x="&' not in text
    assert text.endswith("и еще 5")


def test_no_panel_url_has_plain_bounded_names():
    text = collector._uncov_short([(_srv(15, "bk-01"), ["kube"])], "")
    assert "<a " not in text
    assert text.splitlines()[-1] == "bk-01"


def test_chat_text_keyboard_symbols_only():
    rows = [(_srv(1, "k8s-a"), ["kube", "helper:kubeexpiry-setup"]), (_srv(4, "web-a"), ["docker"])]
    for char in "—–«»…→↳·ёЁ":
        assert char not in collector._uncov_short(rows, BASE)


async def _digest(tmp_path, monkeypatch, missing, custom=False):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path/'panel.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    versions = {"recheck-setup": "0.1", "dbstat-setup": "0.3"}
    rep = {"agent_version": "2.62", "db_engines": ["postgres"],
           "setup_versions": {key: value for key, value in versions.items() if key not in missing}}
    srv = _srv(1, "node-a")
    srv.enabled, srv.last_seen, srv.offline_after_seconds, srv.last_report = True, now, 120, rep
    srv.alert_state = {"uncov": {f"helper:{key}": (now-timedelta(hours=2)).isoformat() for key in missing}}
    async with factory() as session:
        session.add(srv)
        await session.commit()
    sent = []
    async def send(cfg, text, parse_mode=None):
        sent.append(text)
        return []
    monkeypatch.setattr(alerts, "send_alert", send)
    monkeypatch.setattr(collector, "current_setup_versions", lambda: versions)
    if custom:
        async def rules(session):
            return {"uncovered": {"enabled": True, "text": "custom {list}\n{cmd}\n" + "detail " * 1000}}
        monkeypatch.setattr(settings_store, "get_server_alert_rules", rules)
    try:
        # The optional helper is still reported by the panel; only its chat notification is omitted.
        visible = {key for key, _ in collector.gaps(rep, versions)}
        await collector._track_uncovered(factory, now)
        await collector._daily_uncovered(factory, Settings(alert_webhook="http://hook", panel_url=BASE))
        async with factory() as session:
            stamp = await settings_store.get_uncovered_sent(session)
        # The timestamp is persisted, so another scheduler invocation cannot repeat the summary.
        await collector._daily_uncovered(factory, Settings(alert_webhook="http://hook", panel_url=BASE))
        return sent, visible, stamp
    finally:
        await engine.dispose()


async def test_optional_recheck_installation_does_not_notify_the_fleet(tmp_path, monkeypatch):
    sent, visible, stamp = await _digest(tmp_path, monkeypatch, {"recheck-setup"})
    assert "helper:recheck-setup" in visible
    assert sent == [] and stamp == 0


async def test_real_gap_notifies_once_without_optional_noise_and_keeps_full_history(tmp_path, monkeypatch):
    sent, _, stamp = await _digest(tmp_path, monkeypatch, {"recheck-setup", "dbstat-setup"})
    assert stamp > 0 and len(sent) == 1
    short, full = str(sent[0]), alerts.full_text(sent[0])
    assert len(short.splitlines()) == 3 and "dbstat-setup" in short
    assert "recheck-setup" not in short and "ansible-playbook" not in short
    assert "инвентарь СУБД" in full and "kervax_helpers.yml -l 'node-a'" in full
    assert len(alerts._digest_line(sent[0]).splitlines()) == 1
    assert "dbstat-setup" in alerts._digest_line(sent[0])


async def test_custom_long_template_is_retained_in_history_not_in_chat(tmp_path, monkeypatch):
    sent, _, stamp = await _digest(tmp_path, monkeypatch, {"dbstat-setup"}, custom=True)
    assert stamp > 0 and len(sent) == 1
    assert len(_plain(sent[0])) < 200 and len(sent[0].splitlines()) == 3
    assert "custom" not in sent[0] and "ansible" not in sent[0]
    assert "custom" in alerts.full_text(sent[0]) and len(alerts.full_text(sent[0])) > 4000
