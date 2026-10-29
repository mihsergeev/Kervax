"""Починили: алерт закрывается сейчас, счет по окну начинается с отметки, беда по состоянию, которая
еще видна, отметку не принимает (app/fixed.py)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, fixed, oom, restarts, tg_bot, tg_buttons
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AuditLog, Server, ServerMetric, TgAlert, User

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _rep(rc=0):
    return {"uptime_seconds": 100000,
            "docker": {"access": True, "containers": [{"name": "ollama", "state": "running", "restarts": rc}]}}


def _srv(state=None, fixed_marks=None, rep=None):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[], alert_fixed=fixed_marks)


def _restarts(n, start=NOW - timedelta(hours=11)):
    return {"restarts": {"docker:ollama": {"rc": 50, "ev": [[(start + timedelta(hours=i)).isoformat(), 1]
                                                            for i in range(n)]}}}


def _mark(at=NOW, **kw):
    return {"text": "", "by": "admin", "at": at.isoformat(), "done": None, **kw}


# ---------------------------------------------------------------- счет с отметки


def test_restarts_count_from_the_mark():
    s = _srv(_restarts(11))
    assert [x["key"] for x in restarts.problems(s, NOW)] == ["docker:ollama"]
    # отметили: одиннадцать перезапусков за сутки больше не в счет, ни в алерте, ни в "Что сломано"
    s = _srv(_restarts(11), {"docker_restarts": _mark(NOW - timedelta(minutes=1))})
    assert restarts.problems(s, NOW) == []
    assert collector._server_conditions(s, NOW)["docker_restarts"][0] == 0
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "docker_restarts"]
    # раздел карточки помнит сутки как есть
    assert restarts.view(s, NOW) == {"docker:ollama": 11}
    # после отметки снова пять раз - снова беда
    st = _restarts(5, NOW - timedelta(minutes=50))
    st["restarts"]["docker:ollama"]["ev"] += _restarts(11)["restarts"]["docker:ollama"]["ev"]
    s = _srv(st, {"docker_restarts": _mark(NOW - timedelta(hours=1))})
    assert [x["n"] for x in restarts.problems(s, NOW)] == [5]


def test_oom_and_jobs_and_unit_loop_from_the_mark():
    ts = (NOW - timedelta(hours=2)).timestamp()
    kills = [{"ts": ts - d, "pid": 10 + d, "name": "ollama", "kind": "global", "anon": 900000} for d in (0, 3600)]
    rep = {"extras": {"oom": {"v": 1, "ts": NOW.timestamp() - 20, "kills": kills, "units": []}},
           "clock_unix": NOW.timestamp(),
        "kube": {"access": True, "failed_jobs": [{"ns": "shop", "name": "migrate", "ts": int(ts)}]}}
    s = _srv(rep=rep)
    assert oom.problem(s, NOW) is not None  # два OOM за сутки видны
    jobs = [p for p in collector.server_problems(s, NOW) if p["kind"] == "kube_job"]
    assert jobs  # упавший Job виден
    s = _srv(rep=rep, fixed_marks={"oom": _mark(NOW - timedelta(hours=1)), "kube_job": _mark(NOW - timedelta(hours=1))})
    assert oom.problem(s, NOW) is None
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] in ("oom", "kube_job")]
    hits = {"hits": [[(NOW - timedelta(minutes=m)).isoformat(), 1] for m in (2, 5, 9)]}
    assert len(collector.unit_loop_hits(hits, NOW)) == 3
    assert collector.unit_loop_hits(hits, NOW, NOW - timedelta(minutes=6)) == hits["hits"][:2]


def test_log_errors_hot_since_the_mark():
    from app import log_errors

    st = {"logerr_ev": {"docker:api": {"hot": (NOW - timedelta(hours=3)).isoformat()}},
          "log_errors_sig": ["docker:api"]}
    s = _srv(st)
    assert log_errors._hot_for(s, "docker:api", NOW) == 3 * 3600
    assert log_errors._held(s) == {"docker:api"}
    s = _srv(st, {"log_errors": _mark(NOW - timedelta(minutes=10))})
    assert log_errors._hot_for(s, "docker:api", NOW) == 600
    assert log_errors._held(s) == set()  # отметка не закрыта: объявленное не держит пониженный порог
    s = _srv(st, {"log_errors": _mark(NOW - timedelta(minutes=10), done=NOW.isoformat())})
    assert log_errors._held(s) == {"docker:api"}


def test_recovery_text_and_kinds():
    assert fixed.recovery_text("docker_restarts", _mark(text="обновил образ")) == \
        "Docker: часто перезапускается - починили (admin: обновил образ)"
    assert fixed.recovery_text("units", _mark()) == "Systemd: упал юнит - починили (admin)"
    # то, что починенным не отмечается: недоступность, сроки, бэкапы, проверки доступа
    for k in ("offline", "backup_failed", "web_cert", "access_change", "exposed", "reboot"):
        assert k not in fixed.KINDS
    assert not (fixed.WINDOW & fixed.STATE)
    assert fixed.expired(_mark(NOW - timedelta(hours=25)), NOW) and not fixed.expired(_mark(), NOW)


def test_frontend_list_matches():
    """Кнопка на фронте - у тех же видов (frontend/src/FixedDialog.tsx FIX_KINDS)."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "FixedDialog.tsx").read_text(encoding="utf-8")
    body = re.search(r"FIX_KINDS = new Set\(\[(.*?)\]\)", src, re.S).group(1)
    assert set(re.findall(r"'([a-z_0-9]+)'", body)) == set(fixed.KINDS)


# ---------------------------------------------------------------- цикл алертов


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'f.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_restarts_alert_closes_now_and_comes_back_after_new_restarts(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    t0 = datetime.now(timezone.utc) - timedelta(hours=12)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=t0,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=_rep(0)))
        await s.commit()
    sent: list = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(t, rc, **kw):
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen, srv.last_report = t, _rep(rc)
            for k, v in kw.items():
                setattr(srv, k, v)
            await s.commit()
        await collector.evaluate_servers(factory, cfg, t)

    # контейнер падает раз в час: к пятому разу - алерт
    for i in range(7):
        await tick(t0 + timedelta(hours=i), min(i, 6))
    kinds = [getattr(m, "kind", "") for m in sent]
    assert kinds.count("docker_restarts") == 1, sent
    # починили: отбой в следующем тике, без получаса выдержки, с тем, кто отметил
    t_fix = t0 + timedelta(hours=6, minutes=10)
    await tick(t_fix, 6, alert_fixed={"docker_restarts": _mark(t_fix, text="поднял лимит")})
    rec = [m for m in sent if getattr(m, "recovery", False) and m.kind == "docker_restarts"]
    assert len(rec) == 1 and "починили (admin: поднял лимит)" in rec[0], sent
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert int(srv.alert_state.get("docker_restarts") or 0) == 0
        assert srv.alert_fixed["docker_restarts"]["done"]
        assert not [p for p in collector.server_problems(srv, t_fix) if p["kind"] == "docker_restarts"]
    # повторилось после отметки: пять новых перезапусков - алерт как обычно, не "тихий повтор"
    n_before = len(sent)
    for i in range(1, 7):
        await tick(t_fix + timedelta(minutes=20 * i), 6 + i)
    again = [m for m in sent[n_before:] if getattr(m, "kind", "") == "docker_restarts" and not m.recovery]
    assert len(again) == 1 and "5 раз за сутки" in again[0], sent[n_before:]
    # отметка отслужила сутки - уходит
    await tick(t_fix + timedelta(hours=25), 12)
    async with factory() as s:
        assert (await s.execute(select(Server))).scalars().one().alert_fixed is None
    await engine.dispose()


def _disk(pct: int) -> dict:
    return {"disks": [{"mount": "/", "used": pct, "total": 100}]}


async def test_state_not_fixed_yet_then_closes_without_settle(client, auth_headers, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = Server(name="web-a", token_hash="k1", enabled=True, backup_not_required=True, last_seen=now,
                     last_report=_disk(96), cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=85,
                     disk_alert_percent=90, disk_crit_percent=95, alert_sustain_seconds=0, offline_after_seconds=120,
                     known_issues={"disk": {"text": "чистим", "by": "admin", "at": now.isoformat(), "until": None}},
                     alert_state={"disk": 3, "disk_from": now.isoformat(),
                                  "disk_since": (now - timedelta(hours=1)).isoformat()})
        s.add(srv)
        await s.commit()
        sid = srv.id
    # диск все еще полон: честно говорим, что видно, отметки нет
    r = await client.post(f"/api/servers/{sid}/fixed/disk", json={"text": "почистил"}, headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"].startswith("Еще не починено:"), r.text
    assert "96" in r.json()["detail"]
    # не отмечается вовсе
    assert (await client.post(f"/api/servers/{sid}/fixed/offline", json={}, headers=auth_headers)).status_code == 400
    # почистили: диск 40%, а алерт держит выдержка отбоя - отметка принимается
    async with f() as s:
        srv = await s.get(Server, sid)
        srv.last_report = _disk(40)
        await s.commit()
    r = await client.post(f"/api/servers/{sid}/fixed/disk", json={"text": "почистил"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    # известная проблема того же вида снята: беды нет
    assert r.json()["known_issues"] is None
    async with f() as s:
        srv = await s.get(Server, sid)
        assert srv.alert_fixed["disk"]["by"] and srv.alert_fixed["disk"]["text"] == "почистил"
        log = list(await s.scalars(select(AuditLog).where(AuditLog.action == "alert_fixed")))
        assert log and log[0].detail == "Диск - почистил"
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    await collector.evaluate_servers(f, Settings(alert_webhook="http://hook"), now)
    rec = [m for m in sent if m.kind == "disk" and m.recovery]
    assert len(rec) == 1 and "починили" in rec[0]
    async with f() as s:
        srv = await s.get(Server, sid)
        assert int(srv.alert_state.get("disk") or 0) == 0 and not srv.alert_state.get("disk_calm")


async def test_web_window_starts_at_the_mark(tmp_path):
    engine, factory = await _panel(tmp_path)
    async with factory() as s:
        srv = Server(name="web-a", token_hash="x", enabled=True)
        s.add(srv)
        await s.commit()
        for m in range(10):
            s.add(ServerMetric(server_id=srv.id, ts=NOW - timedelta(minutes=m), web_rpm=100, web_5xx=40))
        await s.commit()
        full = await collector.web_error_window(s, NOW)
        cut = await collector.web_error_window(s, NOW, {srv.id: NOW - timedelta(minutes=3, seconds=30)})
        empty = await collector.web_error_window(s, NOW, {srv.id: NOW + timedelta(seconds=1)})
    assert full[srv.id][2] == 10 and cut[srv.id][2] == 4
    assert empty[srv.id] == (0.0, 0.0, 0, {}, 0.0)  # пусто - нули, а не пропуск
    await engine.dispose()


# ---------------------------------------------------------------- кнопка в Telegram


async def test_tg_fixed_button(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    kb = tg_buttons.keyboard(1, "docker_restarts")
    assert any(b["text"] == "✅ Починили" and b["callback_data"] == "kx:1:fx" for r in kb["inline_keyboard"] for b in r)
    assert not any(b["text"] == "✅ Починили" for r in tg_buttons.keyboard(1, "backup_failed")["inline_keyboard"]
                   for b in r)
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = Server(name="web-a", token_hash="x", enabled=True, last_seen=now,
                     alert_state={**_restarts(11, now - timedelta(hours=11)), "docker_restarts": 1},
                     last_report=_rep(11), offline_after_seconds=120)
        s.add(srv)
        s.add(User(username="ivan", password_hash="x", role="operator", tg_chat_id="555"))
        await s.commit()
        row = TgAlert(ref=f"srv:{srv.id}", kind="docker_restarts", target="web-a", text="⚠️🐳 web-a",
                      messages=[{"chat": "-100", "id": 7}])
        s.add(row)
        await s.commit()
        aid, sid = row.id, srv.id
    calls = []

    async def fake_call(cfg, method, payload, timeout=15):
        calls.append((method, payload))
        return {"ok": True}

    async def fake_edit(cfg, chat, mid, text, markup):
        calls.append(("edit", text, markup))

    monkeypatch.setattr(tg_bot, "_call", fake_call)
    monkeypatch.setattr("app.alerts.tg_edit", fake_edit)
    cfg = {"telegram_token": "T", "telegram_chat": "-100", "tz": "Europe/Moscow"}
    cq = {"id": "q1", "data": f"kx:{aid}:fx", "from": {"id": 555}, "message": {"chat": {"id": -100}}}
    await tg_bot.handle_callback(f, cfg, cq, now)
    async with f() as s:
        srv = await s.get(Server, sid)
        assert srv.alert_fixed["docker_restarts"]["by"] == "ivan"
        log = list(await s.scalars(select(AuditLog).where(AuditLog.action == "alert_fixed")))
        assert log
    edit = [c for c in calls if c[0] == "edit"][-1]
    assert "✅ ivan: починили" in edit[1]
    assert not any(b["text"] == "✅ Починили" for r in edit[2]["inline_keyboard"] for b in r)
