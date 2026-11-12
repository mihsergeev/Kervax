"""Отложенный OOM не выглядит новым событием даже после сокращения для Telegram."""
import html
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import alerts, collector, oom, srv_short
from tests.test_notification_text import bounded

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TZ = timezone(timedelta(hours=3))
TS = NOW.timestamp()


def _report(*events):
    kills = [{"ts": at, "pid": i + 1, "name": name, "kind": kind, "memcg": "",
              "oom_memcg": "", "anon": 1024, "uid": 0, "user": "root", "top": []}
             for i, (at, name, kind) in enumerate(events)]
    return {"clock_unix": TS, "extras": {"oom": {"v": 1, "ts": TS - 10, "kills": kills, "units": []}}}


def _server(rep):
    return SimpleNamespace(oom_total=len(rep["extras"]["oom"]["kills"]), oom_victim="",
                           last_report=rep, alert_mutes=[], name="node-a")


def _step(rep, state):
    return oom.alert_step(_server(rep), state, rep, NOW, TZ)


def _assert_notice(short, at):
    assert short[0].startswith(f"Отложенное уведомление: событие в {oom.hhmm(at, TZ)} - ")


@pytest.mark.parametrize("kind", ["global", "memcg", "oomd"])
def test_delayed_first_notice_names_actual_event_time(kind):
    at = TS - 28 * 60
    rep = _report((at, "worker", kind))
    ep = oom.episodes(oom.records(rep["extras"]["oom"]))[-1]
    fire, done, _ = _step(rep, {"oom_end": 0.0})
    _assert_notice(done["oom_short"], at)
    assert fire == (oom.level(ep, set()), oom.episode_text(
        ep, TZ, set(), 1, oom.names_of(rep), late=oom.late_text(ep, TS)))
    assert done["oom_ep"] == ep["id"] and done["oom_held"] == []


@pytest.mark.parametrize("age", [28 * 60, 2 * 60])
def test_held_repeat_is_delayed_even_if_close_to_end_of_repeat_window(age):
    at = TS - age
    rep = _report((at - 180, "worker", "global"), (at, "worker", "global"))
    eps = oom.episodes(oom.records(rep["extras"]["oom"]))
    state = {"oom_end": at, "oom_seen": 2, "oom_sent": TS - oom.REPEAT_S,
             "oom_sent_lvl": 1, "oom_held": [e["id"] for e in eps]}
    fire, done, _ = _step(rep, state)
    _assert_notice(done["oom_short"], at)
    assert fire == (1, oom.episode_text(eps[-1], TZ, set(), 2, oom.names_of(rep),
                                     more=1, late=oom.late_text(eps[-1], TS, held=True)))
    assert done["oom_ep"] == eps[-1]["id"] and done["oom_held"] == []
    assert done["oom_sent"] == TS
    # После доставки тот же эпизод и счетчик повторно не отправляются.
    assert _step(rep, {**state, **done})[0] is None


def test_held_repeat_without_details_does_not_invent_event_time():
    rep = _report()
    fire, done, _ = _step(rep, {"oom_end": TS - 100, "oom_sent": TS - oom.REPEAT_S,
                              "oom_sent_lvl": 1, "oom_held": ["c2", "missing-episode"]})
    assert fire == (1, "нехватка памяти (OOM) повторялась: с прошлого сообщения еще 2 раз")
    assert done["oom_short"] == ("Отложенное уведомление: повторы OOM", ["Время событий неизвестно"])
    assert "oom_ep" not in done and done["oom_held"] == []


@pytest.mark.parametrize("recent,important", [(False, False), (True, False), (True, True)])
def test_new_episode_and_escalation_keep_their_fresh_text_and_hold_policy(recent, important):
    at = TS - 120
    rep = _report((at, "mysqld" if important else "worker", "global"))
    state = {"oom_end": TS - 1800, "oom_sent": TS - (180 if recent else oom.REPEAT_S),
             "oom_sent_lvl": 1, "oom_held": ["older-episode"]}
    fire, done, apply = _step(rep, state)
    if recent and not important:
        assert fire is None and done == {}
        assert apply["oom_held"][0] == "older-episode" and len(apply["oom_held"]) == 2
        return
    assert fire[0] == (2 if important else 1)
    assert done["oom_short"][0] == oom.WHAT_HOST
    assert "Отложенное" not in done["oom_short"][0] and "с опозданием" not in fire[1]
    assert done["oom_held"] == [] and done["oom_sent"] == TS


@pytest.mark.parametrize("kind", ["global", "memcg", "oomd"])
def test_compact_html_preserves_delayed_meaning_time_and_delivery_metadata(kind):
    at = TS - 28 * 60
    name = 'worker<&"\n' * 100
    rep = _report(*[(at + i, str(i) + name, kind) for i in range(3)])
    fire, done, _ = _step(rep, {"oom_end": 0.0})
    server = _server(rep)
    short = srv_short.short("oom", {"items": done["oom_short"]}, server, fire[1], fire[0])
    url = "https://panel.example.test/?server=1&oom=" + done["oom_ep"]
    msg = collector._server_alert_text("oom", 'node<&"', fire[1], url, level=fire[0],
                                       group="ops", ref="srv:1", short=short)
    msg.mutes = ["oom:svc:worker"]
    delivered = alerts.short_message(msg, "HTML")
    assert str(delivered) != str(msg), "проверяем смысл после фактического сокращения"
    parsed = bounded(delivered)
    assert f"Отложенное уведомление: событие в {oom.hhmm(at, TZ)}" in parsed.text
    assert '<&"' in parsed.text and "&lt;" in delivered and "&amp;" in delivered
    assert parsed.links == [url]
    assert (delivered.kind, delivered.section, delivered.group, delivered.ref, delivered.mutes) == (
        "oom", "servers", "ops", "srv:1", ["oom:svc:worker"])
    assert alerts.full_text(delivered) == alerts.full_text(msg)
    assert fire[1] in html.unescape(alerts.full_text(delivered))
    digest = bounded(alerts._digest([delivered]))
    assert "отложенное уведомление" in digest.text.lower() and oom.hhmm(at, TZ) in digest.text
    assert url in digest.links
