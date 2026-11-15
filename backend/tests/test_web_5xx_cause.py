"""Minority error-log entries cannot explain unrelated application HTTP errors."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import alert_what, collector, srv_short, web_upstream
from app.alerts import full_text
from app.models import Server, ServerMetric, WebErrorSample
from tests.test_web_upstream import _panel, _sent


REFUSED = {"u": "backend:8200", "k": "refused", "n": 1}


def _where(count=1, errors=74, kind="refused"):
    it = dict(REFUSED, k=kind, n=count)
    return {"logs": [["nginx-secrets", errors]], "codes": {"503": errors - 1, "502": 1},
            "paths": {"/api/login": errors - 1}, "ups": {(it["u"], kind): it}}


def _short(where):
    return srv_short.short("web_5xx", {"value": 10, "where_raw": where}, SimpleNamespace(last_report={}))


@pytest.mark.parametrize("kind", ["refused", "timeout", "noep", "resolve", "ssl"])
def test_minority_log_entry_is_not_the_cause_of_http_503(kind):
    where = _where(kind=kind)
    assert web_upstream.main_cause(where) is None
    what, facts = _short(where)
    assert what == "10% запросов с ошибкой 5xx"
    assert facts == ["nginx-secrets: 503", "Чаще всего: /api/login"]
    detail = collector.web_where_text(where)
    assert "Коды: 503 - 73, 502 - 1" in detail
    assert "В журнале nginx:" in detail and " - 1" in detail
    assert "Почему:" not in detail


@pytest.mark.parametrize(("count", "errors", "expected"), [
    (1, 74, False), (79, 100, False), (80, 100, True), (100, 100, True),
    (101, 100, False), (1, 0, False), (0, 0, False),
    (float("nan"), 100, False), (1, float("inf"), False), (True, 1, False),
])
def test_dominant_count_needs_consistent_totals(count, errors, expected):
    assert web_upstream.dominates(count, errors) is expected


def test_dominant_connection_failure_is_still_explained():
    where = _where(count=74)
    where["codes"] = {"502": 74}
    assert "бэкенд не принимает соединения" in _short(where)[1][0]
    assert "Почему: backend:8200 не принимает соединения - 74" in collector.web_where_text(where)


def test_unknown_totals_and_retries_cannot_establish_a_cause():
    assert web_upstream.main_cause({"ups": _where()["ups"]}) is None
    assert web_upstream.main_cause(_where(count=100)) is None


def test_another_logs_upstream_is_not_attached_to_the_displayed_site():
    where = _where(count=9, errors=10)
    where["logs"] = [["nginx-secrets", 9], ["nginx-other", 1]]
    where["ups_by_log"] = {"nginx-secrets": {}, "nginx-other": where["ups"]}
    assert web_upstream.main_cause(where) is None
    assert _short(where)[1][0] == "nginx-secrets: 503"


def test_last_minute_counts_include_http_errors_without_error_log_entries():
    extras = {"web-rate": {"logs": [
        {"name": "nginx-secrets", "e5": 73, "c5": {"503": 73}},
        {"name": "nginx-other", "e5": 1, "c5": {"502": 1}, "up": [REFUSED]},
    ]}}
    evidence = web_upstream.block_where(extras, label_key=lambda x: x["name"])
    assert sum(x[1] for x in evidence["logs"]) == 74
    assert evidence["codes"] == {"503": 73, "502": 1}
    assert web_upstream.main_cause(evidence) is None
    assert web_upstream.block_acc(extras) == _where()["ups"]
    # Muting the failing HTTP log must not leave its denominator behind.
    unmuted = web_upstream.block_where(extras, {"nginx-secrets"}, lambda x: x["name"])
    assert web_upstream.main_cause(unmuted) == REFUSED


def test_malformed_optional_counts_do_not_break_current_summary():
    extras = {"web-rate": {"logs": [
        {"name": "a", "e5": "unknown", "c5": {"503": "bad"}, "up": [REFUSED]},
        {"name": "b", "e5": None, "c5": ["bad"]},
    ]}}
    evidence = web_upstream.block_where(extras)
    assert web_upstream.main_cause(evidence) is None
    assert web_upstream.block_acc(extras) == _where()["ups"]


@pytest.mark.parametrize("section", ["Почему", "В журнале nginx"])
def test_history_retains_minority_counts_instead_of_claiming_tcp_caused_503(section):
    body = ("10.00% запросов за 15 минут закончились ошибкой 5xx (74 из 740). "
            "Больше всего: nginx-secrets - 74. Коды: 503 - 73, 502 - 1. "
            f"{section}: backend:8200 не принимает соединения - 1. Чаще всего падает: /api/login - 73")
    assert alert_what._web5xx(body, []) == [
        ("ошибки 5xx на nginx-secrets",
         "10.00% запросов, чаще /api/login, в журнале nginx: 1 - backend:8200 не принимает соединения")]


def test_minority_no_endpoints_is_not_promoted_to_the_whole_incident():
    body = ("10.00% запросов за 15 минут закончились ошибкой 5xx (74 из 740). "
            "Коды: 503 - 74. Почему: нет живых подов у сервиса shop/api (ingress shop, api.example.com) - 1")
    what, note = alert_what._web5xx(body, [])[0]
    assert what == "ошибки 5xx"
    assert "в журнале nginx: 1 - нет живых подов" in note


@pytest.mark.parametrize("old_inside_window", [True, False])
async def test_many_http_503_and_one_old_refused_still_alert_without_false_cause(
        tmp_path, monkeypatch, old_inside_window):
    engine, factory = await _panel(tmp_path, "mixed.db")
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                           last_seen=now, last_report={"cpu_percent": 20}, cpu_alert_percent=0,
                           mem_alert_percent=0, disk_alert_percent=0, web_5xx_alert_percent=0.05))
        await session.commit()
        for minute in range(15):
            errors = 3 if minute == 14 else 5  # 73 actual application responses
            ts = now - timedelta(minutes=minute)
            session.add(ServerMetric(server_id=1, ts=ts, cpu_percent=20, web_rpm=1000, web_5xx=errors))
            session.add(WebErrorSample(server_id=1, ts=ts, src_ts=int(ts.timestamp()), e5=errors, rpm=1000,
                                       log="docker:nginx-secrets", label="nginx-secrets", codes={"503": errors},
                                       paths=[{"p": "/api/login", "n": errors}]))
        old = now - timedelta(minutes=14 if old_inside_window else 16)
        session.add(WebErrorSample(server_id=1, ts=old, src_ts=int(old.timestamp()), e5=1, rpm=0,
                                   log="docker:nginx-secrets", label="nginx-secrets", codes={"502": 1},
                                   ups=[REFUSED]))
        await session.commit()
        where = await collector.web_error_where(session, 1, now)
    assert where["codes"] == ({"503": 73, "502": 1} if old_inside_window else {"503": 73})
    assert web_upstream.main_cause(where) is None
    sent = [x for x in await _sent(factory, now, monkeypatch) if "5xx" in x]
    assert len(sent) == 1  # no suppression of the real 5xx incident
    assert "nginx-secrets: 503" in sent[0] and "не принимает соединения" not in sent[0]
    detail = full_text(sent[0])
    assert "Коды: 503 - 73" in detail
    assert ("В журнале nginx: backend:8200 не принимает соединения - 1" in detail) is old_inside_window
    assert "Почему:" not in detail
    await engine.dispose()
