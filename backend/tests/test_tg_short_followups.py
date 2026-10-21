"""Короткие алерты, доделки после 1.5.0: суть ошибки Job из лога, без повторов уже объявленного,
шторм больше 30 серверов в полном тексте."""

from app import alerts, job_logs, srv_short
from app.collector import _server_alert_text


def test_job_gist_python_traceback():
    raw = ("2026-10-21T10:00:00Z Starting migration\n"
           "Traceback (most recent call last):\n"
           '  File "m.py", line 3, in <module>\n'
           "psycopg2.OperationalError: could not connect to server: Connection refused\n")
    assert job_logs.gist(raw) == "psycopg2.OperationalError: could not connect to server: Connection refused"


def test_job_gist_skips_stack_frames_and_level():
    raw = ("Error: ENOENT: no such file or directory, open '/app/x.json'\n"
           "    at Object.openSync (node:fs:1:2)\n"
           "    at main (/app/error.js:3:4)\n")
    assert job_logs.gist(raw) == "ENOENT: no such file or directory, open '/app/x.json'"
    assert job_logs.gist('level=error msg="backup failed: exit status 1"') == "backup failed: exit status 1"


def test_job_gist_empty_without_error_line():
    assert job_logs.gist("Done.\n") == ""
    assert job_logs.gist("") == ""


def test_job_gist_long_line_is_cut():
    g = job_logs.gist("fatal: " + "x" * 300)
    assert len(g) <= 103 and g.endswith("...")


def _job_msg():
    return _server_alert_text("kube_job", "k8s-a", "упал Job: app/migrate", "https://p/1", level=2,
                              ref="srv:1", short=("Упал Job", ["app/migrate (BackoffLimitExceeded)"]))


def test_job_alert_gets_error_gist_line_and_full_log():
    logs = [{"pod": "migrate-abcde", "reason": "Error", "ok": True,
             "log": "start\nERROR: relation \"users\" does not exist\n"}]
    m = job_logs.attach(_job_msg(), logs)
    lines = str(m).split("\n")
    assert lines[1:] == ["<b>Упал Job</b>", "app/migrate (BackoffLimitExceeded)",
                         "relation &quot;users&quot; does not exist"]
    assert "<pre>" in m.full and "<pre>" not in str(m)


def test_job_alert_without_error_line_stays_short():
    logs = [{"pod": "migrate-abcde", "reason": "Error", "ok": True, "log": "Done.\n"}]
    m = job_logs.attach(_job_msg(), logs)
    assert str(m) == str(_job_msg()) and "Done." in m.full


def test_workers_followup_lists_only_new():
    wp = [{"key": "worker-a:billing", "name": "billing", "state": "loop", "where": ""},
          {"key": "worker-a:cleanup", "name": "cleanup", "state": "loop", "where": ""},
          {"key": "worker-a:digest", "name": "digest", "state": "loop", "where": ""}]
    what, facts = srv_short.short("workers", {"items": wp, "fresh": {"worker-a:digest"}}, None)
    text = "\n".join([what] + facts)
    assert "digest" in text and "billing" not in text and "cleanup" not in text and "Еще" not in text


def test_followup_without_known_keys_keeps_all():
    wp = [{"key": "a", "name": "billing", "state": "loop", "where": ""},
          {"key": "b", "name": "cleanup", "state": "loop", "where": ""}]
    _what, facts = srv_short.short("workers", {"items": wp, "fresh": {"zzz"}}, None)
    assert "cleanup" in "\n".join(facts)


def test_storm_full_text_says_how_many_more_after_30():
    msgs = [_server_alert_text("units", f"s{i}", "упал node-exporter.service: код 1", f"https://p/{i}",
                               level=2, ref=f"srv:{i}", short=("Упал node-exporter.service", ["код 1"]))
            for i in range(35)]
    out = alerts.merge_storm(msgs)
    assert len(out) == 1
    assert out[0].full.count("<a href") == 30 and "и еще 5" in out[0].full
    assert str(out[0]).split("\n")[-1].endswith("и еще 29")
