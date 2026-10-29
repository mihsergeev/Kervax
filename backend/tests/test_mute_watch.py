"""Жизнь заглушек (app/mute_watch.py): есть ли сейчас то, что глушит заглушка, автоснятие тихих временных,
удержание кончившихся при живой проблеме."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app import mute_watch as mw

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class _Srv:
    def __init__(self, mutes=None, snoozes=None, until=None):
        self.alert_mutes = mutes
        self.alert_snoozes = snoozes
        self.snooze_until = until
        self.name = "node-a"


def _iso(d: datetime) -> str:
    return d.isoformat()


def test_keys_skip_decisions_and_expired():
    s = _Srv(mutes=["units", "access:bob", "home:d-old@abc", "osupd:eol", "unit:x.service"],
             snoozes={"kube_pod": _iso(NOW + timedelta(hours=5)), "cpu": _iso(NOW - timedelta(hours=1)),
                      "units": _iso(NOW + timedelta(hours=1))},
             until=NOW + timedelta(days=1))
    k = mw.keys(s, NOW)
    # насовсем главнее снуза того же вида, истекший снуз и решения не в счет
    assert k == {"": _iso(NOW + timedelta(days=1)), "kube_pod": _iso(NOW + timedelta(hours=5)),
                 "units": None, "unit:x.service": None}


def test_shadow_strips_item_mutes_only():
    s = _Srv(mutes=["units", "unit:x.service", "access:bob"], snoozes={"worker:a|b": "x", "cpu": "y"})
    sh = mw.shadow(s)
    assert sh.alert_mutes == ["units", "access:bob"] and sh.alert_snoozes == {"cpu": "y"}
    assert sh.name == "node-a"


def test_judge_wide_item_and_unknown():
    conds = {"cpu": (0, {}), "units": (2, {"sig": ["a.service"]}), "disk": (0, {"raw": 1, "detail": "Диск 86%"})}
    probs = [{"kind": "units", "text": "a.service: упал", "mute": "unit:a.service"},
             {"kind": "kube_pod", "text": "поды не работают: ns/web (CrashLoopBackOff)", "mute": "kube_pod"}]
    shadow = probs + [{"kind": "units", "text": "x.service: упал", "mute": "unit:x.service"}]
    present = {"kube_pod:": {"ns/web": "ns/web-1 (CrashLoopBackOff)"}}
    j = lambda k, sh=shadow, pr=present: mw.judge(k, conds, probs, sh, pr, {"unit:x.service"})  # noqa: E731
    assert j("units") == (True, "a.service: упал")
    assert j("cpu") == (False, "")
    # уровень без строки в "Что сломано": что именно - из условия
    assert j("disk@1") == (True, "Диск 86%")
    assert j("kube_pod") == (True, "поды не работают: ns/web (CrashLoopBackOff)")
    assert j("unit:x.service") == (True, "x.service: упал")
    assert j("unit:y.service") == (False, "")
    assert j("kube_pod:ns/web") == (True, "ns/web-1 (CrashLoopBackOff)")
    assert j("kube_pod:ns/api") == (False, "")
    # кластер не виден, вид без данных, событие без состояния: панель не судит
    assert j("kube_pod:ns/web", pr={}) is None
    assert j("unit:x.service", sh=None) is None
    assert j("reboot") is None and j("web_5xx:abc") is None
    assert j("")[0] is True


def test_judge_docker_wide_without_item_muted():
    present = {"docker_down:": {"web": "контейнер web лежит"}}
    assert mw.judge("docker_down", {}, [], None, present, {"docker_down:web"}) == (False, "")
    assert mw.judge("docker_down", {}, [], None, present, set()) == (True, "контейнер web лежит")


def test_live_step_tracks_since_and_first_pass():
    until = _iso(NOW + timedelta(days=2))
    live = mw.live_step(None, {"units": until}, {"units": (True, "a: упал")}, NOW)
    # застали уже стоящей: начала не знаем
    assert live == {"units": {"seen": None, "u": until, "on": 1, "t": _iso(NOW), "w": "a: упал"}}
    later = NOW + timedelta(hours=1)
    live2 = mw.live_step(live, {"units": until, "cpu": None}, {"units": (False, ""), "cpu": None}, later)
    assert live2["units"]["on"] == 0 and live2["units"]["t"] == _iso(later) and "w" not in live2["units"]
    # новую заглушку панель видит с этого тика; судить не о чем - "есть ли" не пишется
    assert live2["cpu"] == {"seen": _iso(later), "u": None}
    # нода замолчала: прежнее держится
    assert mw.live_step(live2, {"units": until}, {}, later + timedelta(hours=5))["units"]["t"] == _iso(later)


def _plan(cur, live, over=None, conds=None, stored=None, now=NOW, lift=True):
    return mw.plan(cur, live, over, conds or {}, stored if stored is not None else dict(cur), now, lift=lift)


def test_quiet_day_lifts_timed_not_permanent():
    until = _iso(NOW + timedelta(days=3))
    quiet = {"on": 0, "t": _iso(NOW - timedelta(hours=25)), "seen": None, "u": until}
    over, acts = _plan({"kube_pod": until, "units": None}, {"kube_pod": quiet, "units": dict(quiet, u=None)})
    assert over == {} and acts == [("kube_pod", until, None, "заглушка снята: проблемы нет уже сутки")]
    # тихо меньше суток или нода молчит - держится
    assert _plan({"kube_pod": until}, {"kube_pod": dict(quiet, t=_iso(NOW - timedelta(hours=20)))})[1] == []
    assert _plan({"kube_pod": until}, {"kube_pod": quiet}, lift=False)[1] == []


def test_hold_starts_before_end_only_for_long_snooze():
    until = NOW + timedelta(minutes=5)
    cur = {"kube_pod": _iso(until)}
    conds = {"kube_pod": (2, {"sig": ["ns/web"]})}
    long_ = {"on": 1, "t": _iso(NOW - timedelta(days=1)), "seen": _iso(until - timedelta(days=1)), "u": _iso(until)}
    over, acts = _plan(cur, {"kube_pod": long_}, conds=conds)
    ext = _iso(until + timedelta(days=1))
    assert acts == [("kube_pod", _iso(until), ext, "")]
    assert over == {"kube_pod": {"at": _iso(until), "ext": ext, "lvl": 2, "sig": ["ns/web"]}}
    # снуз на час ("напомни позже") - как раньше, сработает в срок
    short = dict(long_, seen=_iso(until - timedelta(hours=1)))
    assert _plan(cur, {"kube_pod": short}, conds=conds) == ({}, [])
    # до конца еще далеко - рано
    assert _plan({"kube_pod": _iso(NOW + timedelta(hours=2))}, {"kube_pod": long_}, conds=conds)[1] == []


def test_hold_release_renew_and_human_change():
    at, ext = _iso(NOW - timedelta(hours=3)), _iso(NOW + timedelta(hours=10))
    over0 = {"kube_pod": {"at": at, "ext": ext, "lvl": 2, "sig": ["ns/web"]}}
    on = {"on": 1, "t": _iso(NOW - timedelta(days=1)), "seen": None, "u": ext}
    conds = {"kube_pod": (2, {"sig": ["ns/web"]})}
    stored = {"kube_pod": ext}
    # держится, пока не хуже
    assert _plan({"kube_pod": ext}, {"kube_pod": on}, over0, conds, stored) == (over0, [])
    # стало хуже: новый под - снимается, алерт пойдет как обычно
    worse = {"kube_pod": (2, {"sig": ["ns/web", "ns/api"]})}
    assert _plan({"kube_pod": ext}, {"kube_pod": on}, over0, worse, stored) == (
        {}, [("kube_pod", ext, None, "заглушка снята: стало хуже (новое: ns/api)")])
    # проблема ушла 10 минут назад - молча снимается
    gone = dict(on, on=0, t=_iso(NOW - timedelta(minutes=11)))
    assert _plan({"kube_pod": ext}, {"kube_pod": gone}, over0, {"kube_pod": (0, {})}, stored)[1] == [
        ("kube_pod", ext, None, "заглушка снята: проблема ушла")]
    # ушла только что - ждем
    assert _plan({"kube_pod": ext}, {"kube_pod": dict(gone, t=_iso(NOW))}, over0, conds, stored)[1] == []
    # человек продлил сам или вернул алерты - пометка уходит, сроки не трогаем
    assert _plan({"kube_pod": _iso(NOW + timedelta(days=7))}, {"kube_pod": on}, over0, conds,
                 {"kube_pod": _iso(NOW + timedelta(days=7))}) == ({}, [])
    assert _plan({}, {}, over0, conds, {}) == ({}, [])
    # шаг кончается - продлевается еще на сутки
    soon = _iso(NOW + timedelta(minutes=30))
    over1 = {"kube_pod": dict(over0["kube_pod"], ext=soon)}
    o, acts = _plan({"kube_pod": soon}, {"kube_pod": on}, over1, conds, {"kube_pod": soon})
    new = _iso(NOW + timedelta(days=1))
    assert acts == [("kube_pod", soon, new, "")] and o["kube_pod"]["ext"] == new and o["kube_pod"]["at"] == at


def test_server_snooze_worse_by_new_kind():
    snap = mw.snapshot("", {"units": (2, {}), "cpu": (0, {})})
    assert snap == {"kinds": {"units": 2}}
    assert mw.worse("", snap, {"units": (2, {}), "offline": (0, {})}) == ""
    assert mw.worse("", snap, {"units": (2, {}), "offline": (1, {})}) == "новое: offline"


def test_problem_kinds_match_server_problems():
    """PROBLEM_KINDS - все виды, которые выдает "Что сломано" (collector.server_problems)."""
    src = (Path(__file__).resolve().parents[1] / "app" / "collector.py").read_text(encoding="utf-8")
    body = src[src.index("def _server_problems("):src.index("\ndef ", src.index("def _server_problems(") + 10)]
    kinds = set(re.findall(r'\badd\("([a-z_0-9]+)"', body))
    kinds |= {"cpu", "mem", "temp", "conntrack", "disktemp", "throttle", "flux_down", "flux_stale"}  # add(kind, ...)
    assert kinds == set(mw.PROBLEM_KINDS)


def test_decisions_match_frontend():
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "mutes.tsx").read_text(encoding="utf-8")
    consts = dict(re.findall(r"export const (\w+_MUTE) = '([^']+)'", src))
    names = re.search(r"const DECISIONS = \[([^\]]+)\]", src).group(1).replace(" ", "").split(",")
    assert {consts[n] for n in names} | {"home:"} == set(mw.DECISIONS)


async def _env(tmp_path, monkeypatch, **srv):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'mw.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                     cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=80, disk_alert_percent=90,
                     disk_crit_percent=95, created_at=NOW - timedelta(days=30), **srv))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return engine, factory, Settings(alert_webhook="http://hook"), sent


def _report(now: datetime, pct: int) -> dict:
    return {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000,
            "disks": [{"mount": "/", "total": 100 * 1024 ** 3, "used": pct * 1024 ** 3}]}


async def _tick(factory, cfg, now: datetime, pct: int):
    from app import collector
    from app.models import Server

    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_seen = now
        srv.last_report = _report(now, pct)
        await s.commit()
    await collector.evaluate_servers(factory, cfg, now)
    async with factory() as s:
        return (await s.execute(select(Server))).scalars().one()


async def _audit(factory) -> list[str]:
    from app.models import AuditLog

    async with factory() as s:
        return [r.detail for r in await s.scalars(select(AuditLog).where(AuditLog.action == "mute_lifted"))]


async def test_hold_on_expiry_then_silent_release(tmp_path, monkeypatch):
    """Снуз диска на два дня кончается, а диск полон: в минуту окончания ни одного алерта, на главной
    пометка "кончилась". Место освободили - снуз молча уходит, в Telegram ничего."""
    until = NOW + timedelta(days=2)
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, alert_snoozes={"disk": _iso(until)})
    srv = await _tick(factory, cfg, NOW, 99)
    assert sent == [] and srv.alert_state["mute_live"]["disk"]["on"] == 1
    assert "Диск 99%" in srv.alert_state["mute_live"]["disk"]["w"]
    srv = await _tick(factory, cfg, until - timedelta(minutes=5), 99)
    ext = _iso(until + timedelta(days=1))
    assert srv.alert_snoozes["disk"] == ext and srv.alert_state["mute_over"]["disk"]["at"] == _iso(until)
    srv = await _tick(factory, cfg, until + timedelta(minutes=5), 99)
    assert sent == [] and srv.alert_state["mute_over"]["disk"]["ext"] == ext
    t = until + timedelta(minutes=10)
    await _tick(factory, cfg, t, 50)
    srv = await _tick(factory, cfg, t + timedelta(minutes=11), 50)
    assert "disk" not in (srv.alert_snoozes or {}) and not srv.alert_state.get("mute_over")
    assert await _audit(factory) == ["Диск: заглушка снята: проблема ушла"]
    await _tick(factory, cfg, t + timedelta(minutes=13), 50)
    assert sent == []
    await engine.dispose()


async def test_quiet_day_lifts_without_message(tmp_path, monkeypatch):
    until = NOW + timedelta(days=5)
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, alert_snoozes={"disk": _iso(until)},
                                           alert_mutes=["inode"])
    await _tick(factory, cfg, NOW, 50)
    srv = await _tick(factory, cfg, NOW + timedelta(hours=23), 50)
    assert "disk" in srv.alert_snoozes
    srv = await _tick(factory, cfg, NOW + timedelta(hours=24, minutes=1), 50)
    # временная снята, насовсем держится
    assert "disk" not in srv.alert_snoozes and srv.alert_mutes == ["inode"]
    assert srv.alert_state["mute_live"]["inode"]["on"] == 0
    assert await _audit(factory) == ["Диск: заглушка снята: проблемы нет уже сутки"]
    assert sent == []
    await engine.dispose()


async def test_short_snooze_fires_as_before(tmp_path, monkeypatch):
    """Снуз на час, поставленный при работающей панели, - "напомни позже": в срок алерт."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    await _tick(factory, cfg, NOW, 50)
    from app.models import Server

    until = NOW + timedelta(hours=1)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.alert_snoozes = {"disk": _iso(until)}
        await s.commit()
    await _tick(factory, cfg, NOW + timedelta(minutes=1), 99)
    srv = await _tick(factory, cfg, until - timedelta(minutes=5), 99)
    assert srv.alert_snoozes["disk"] == _iso(until) and not srv.alert_state.get("mute_over")
    await _tick(factory, cfg, until + timedelta(minutes=1), 99)
    assert len(sent) == 1 and "99%" in sent[0]
    await engine.dispose()


async def test_hold_released_when_worse(tmp_path, monkeypatch):
    """Удержание при 92% (проблема), диск дошел до 99% (критично): заглушка снимается, и алерт идет
    в этом же тике."""
    until = NOW + timedelta(days=2)
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, alert_snoozes={"disk": _iso(until)})
    await _tick(factory, cfg, NOW, 92)
    await _tick(factory, cfg, NOW + timedelta(hours=2), 92)
    srv = await _tick(factory, cfg, until - timedelta(minutes=5), 92)
    assert srv.alert_state["mute_over"]["disk"]["lvl"] == 2
    srv = await _tick(factory, cfg, until + timedelta(minutes=2), 99)
    assert "disk" not in srv.alert_snoozes
    assert len(sent) == 1 and "99%" in sent[0]
    assert await _audit(factory) == ["Диск: заглушка снята: стало хуже (уровень выше)"]
    await engine.dispose()


def test_view_hides_hold_changed_by_hand():
    ext = _iso(NOW + timedelta(hours=10))
    s = _Srv(snoozes={"kube_pod": ext, "units": _iso(NOW + timedelta(days=1))})
    s.alert_state = {"mute_live": {"kube_pod": {"on": 1, "t": _iso(NOW), "w": "x", "seen": None, "u": ext}},
                     "mute_over": {"kube_pod": {"at": _iso(NOW), "ext": ext},
                                   "units": {"at": _iso(NOW), "ext": _iso(NOW + timedelta(hours=3))}}}
    live, over = mw.view(s)
    assert live == {"kube_pod": {"on": 1, "t": _iso(NOW), "w": "x"}}
    # "units" продлили руками: пометка ушла сразу, не дожидаясь тика сборщика
    assert over == {"kube_pod": {"at": _iso(NOW)}}
