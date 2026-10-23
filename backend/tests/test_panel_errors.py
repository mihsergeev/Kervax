"""Журнал ошибок панели (app/panel_errors.py, api/panel_errors.py): что пишется, что нет, как
складываются дубли, маска секретов, сообщение человека с приложенными ошибками и права."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import panel_errors as pe
from app.api import panel_errors as pe_api


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pe.reset()
    pe_api.reset_limits()
    # без сдерживания частоты в памяти: каждая запись идет в базу (само сдерживание - свой тест)
    monkeypatch.setattr(pe, "_THROTTLE_S", 0.0)
    yield
    pe.reset()
    pe_api.reset_limits()


def _app(client):
    return client._transport.app


async def _rows(client, **where):
    from app.models import PanelError

    await pe.drain()
    async with _app(client).state.session_factory() as s:
        q = select(PanelError)
        for k, v in where.items():
            q = q.where(getattr(PanelError, k) == v)
        return list(await s.scalars(q.order_by(PanelError.id)))


async def _viewer(client, auth_headers):
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "viewer1", "password": "viewer-pass-2026", "role": "viewer"})
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-pass-2026"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_mask_hides_secrets_in_text_and_bodies():
    s = pe.mask_text('REPOPASS_B64=c2VjcmV0 password=hunter2 "token": "abc" https://bk:pw123@h:64101/x '
                     "Authorization: Bearer eyJhbGciOi.abc.def bot 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    for secret in ("c2VjcmV0", "hunter2", '"abc"', "pw123", "eyJhbGciOi", "AAAAAAAAAAAAAAAAAAAA"):
        assert secret not in s, s
    assert "https://bk:***@h:64101/x" in s
    body = pe.mask_obj({"name": "web-1", "password": "x", "repopass": "y", "auth_pass": "z",
                        "nested": {"api_key": "k", "keep": "ok"}, "paths": ["/etc"] * 30})
    assert body["name"] == "web-1" and body["nested"]["keep"] == "ok"
    assert body["password"] == body["repopass"] == body["auth_pass"] == body["nested"]["api_key"] == "***"
    assert len(body["paths"]) == 21  # 20 + "... 10 more"
    assert len(pe.mask_text("x" * 5000)) < 2100


async def test_unhandled_500_is_logged_with_route_user_and_masked_body(client, auth_headers):
    from pydantic import BaseModel

    class In(BaseModel):
        name: str
        password: str

    async def boom(server_id: int, body: In):
        raise RuntimeError(f"cannot connect to postgresql://kervax:{body.password}@db/kervax")

    _app(client).add_api_route("/api/servers/{server_id}/boom", boom, methods=["POST"])
    with pytest.raises(RuntimeError):
        await client.post("/api/servers/7/boom", headers={**auth_headers, "X-Kervax-Page": "/?server=7&sec=backup",
                                                          "X-Kervax-Section": "servers"},
                          json={"name": "web-1", "password": "s3cr3t-pass"})
    rows = await _rows(client, kind="server")
    assert len(rows) == 1
    r = rows[0]
    assert r.status == 500 and r.action == "POST /api/servers/{server_id}/boom"
    assert r.username == "admin" and r.server_id == 7 and r.section == "servers" and r.page == "/?server=7&sec=backup"
    assert r.message.startswith("RuntimeError: cannot connect") and r.version
    assert "Traceback" in r.detail and "boom" in r.detail and '"name": "web-1"' in r.detail
    for where in (r.message, r.detail):
        assert "s3cr3t-pass" not in where


async def test_duplicates_fold_and_resolved_error_comes_back(client):
    f = _app(client).state.session_factory
    a = await pe.record(f, kind="api", action="POST /api/checks/{check_id}/run", status=400, message="монитор 12 не найден")
    b = await pe.record(f, kind="api", action="POST /api/checks/{check_id}/run", status=400, message="монитор 31 не найден")
    other = await pe.record(f, kind="api", action="POST /api/checks/{check_id}/run", status=409, message="монитор 31 не найден")
    assert a == b != other
    rows = await _rows(client)
    assert [x.count for x in rows] == [2, 1]
    assert rows[0].message == "монитор 31 не найден"  # текст последнего случая
    # разобрали - и повторилось: снова неразобранная, счет с нуля
    from app.models import PanelError

    async with f() as s:
        row = await s.get(PanelError, a)
        row.resolved = True
        await s.commit()
    assert await pe.record(f, kind="api", action="POST /api/checks/{check_id}/run", status=400, message="монитор 5 не найден") == a
    r = (await _rows(client))[0]
    assert (r.resolved, r.reopened, r.count) == (False, True, 1)


async def test_same_error_in_a_burst_is_counted_in_memory(client, monkeypatch):
    f = _app(client).state.session_factory
    monkeypatch.setattr(pe, "_THROTTLE_S", 60.0)
    first = await pe.record(f, kind="server", action="GET /api/servers", status=500, message="boom")
    assert first is not None
    for _ in range(4):
        assert await pe.record(f, kind="server", action="GET /api/servers", status=500, message="boom") is None
    assert (await _rows(client))[0].count == 1
    # окно прошло: следующая запись приносит накопленное
    pe._recent[next(iter(pe._recent))][0] -= 120
    await pe.record(f, kind="server", action="GET /api/servers", status=500, message="boom")
    assert (await _rows(client))[0].count == 6


async def test_noisy_codes_are_not_logged(client, auth_headers):
    # 401: вход протух / без токена
    assert (await client.post("/api/checks", json={})).status_code == 401
    # неверный пароль при входе
    assert (await client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass-123"})).status_code == 401
    # 422: форма не прошла проверку
    r = await client.post("/api/checks", headers=auth_headers, json={"name": 5})
    assert r.status_code == 422
    # GET с отказом - это не действие
    assert (await client.get("/api/checks/99999", headers=auth_headers)).status_code == 404
    assert await _rows(client) == []
    # а неудачное действие человека - пишется
    r = await client.delete("/api/checks/99999", headers={**auth_headers, "X-Kervax-Section": "sites"})
    assert r.status_code == 404
    rows = await _rows(client)
    assert len(rows) == 1 and rows[0].kind == "api" and rows[0].status == 404
    assert rows[0].action == "DELETE /api/checks/{check_id}" and rows[0].username == "admin"
    assert rows[0].message and rows[0].section == "sites"


def test_should_record_rules():
    assert pe.should_record("POST", "/api/servers/1/backup/command", 400) == "api"
    assert pe.should_record("POST", "/api/servers/1/backup/command", 401) is None
    assert pe.should_record("PUT", "/api/checks/1", 422) is None
    assert pe.should_record("POST", "/api/auth/2fa/enable", 400) is None
    assert pe.should_record("POST", "/api/panel-errors/report", 429) is None
    assert pe.should_record("GET", "/api/servers", 404) is None
    assert pe.should_record("GET", "/api/servers", 500) == "server"
    # агенты: отказы считает request_watch, сюда - только падение бэкенда
    assert pe.should_record("POST", "/api/agent/report", 401) is None
    assert pe.should_record("GET", "/api/agent/release", 503) is None
    assert pe.should_record("POST", "/api/agent/report", 500) == "server"
    assert pe.should_record("GET", "/api/health", 503) is None


async def test_failed_command_started_by_a_person_lands_in_the_journal(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "db-01"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    r = await client.post(f"/api/servers/{sid}/backup/command", headers=auth_headers,
                          json={"action": "dump_setup", "engine": "pg", "container": "pg-main"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    agent = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/agent/backup-result", headers=agent, json={
        "id": cid, "ok": False, "output": "checking pg-main\nthe dump probe failed, dumps were NOT enabled: "})
    assert r.status_code == 200
    rows = await _rows(client, kind="command")
    assert len(rows) == 1
    x = rows[0]
    assert x.action == "backup dump_setup" and x.username == "admin" and x.server_id == sid
    assert x.server_name == "db-01" and x.section == "backups"
    assert "dump probe failed" in x.message and "checking pg-main" in x.detail
    assert x.extra["params"]["payload"]["engine"] == "pg" and x.extra["params"]["payload"]["container"] == "pg-main"
    # успешная команда и команда планировщика (без created_by) в журнал не идут
    r = await client.post(f"/api/servers/{sid}/backup/command", headers=auth_headers, json={"action": "run_now"})
    await client.post("/api/agent/backup-result", headers=agent, json={"id": r.json()["id"], "ok": True, "output": "ok"})
    from app.models import BackupCommand

    async with _app(client).state.session_factory() as s:
        c = BackupCommand(server_id=sid, action="disk_fix", mode="run", origin="auto")
        s.add(c)
        await s.commit()
        auto_id = c.id
    await client.post("/api/agent/backup-result", headers=agent, json={"id": auto_id, "ok": False, "output": "x"})
    assert len(await _rows(client, kind="command")) == 1


async def test_docker_command_timeout_on_poll_is_logged(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "app-01"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    r = await client.post(f"/api/servers/{sid}/docker/command", headers=auth_headers,
                          json={"container": "web", "action": "restart"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    from app.models import DockerCommand

    async with _app(client).state.session_factory() as s:
        c = await s.get(DockerCommand, cid)
        c.status = "running"
        c.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
    r = await client.get(f"/api/servers/{sid}/docker/command/{cid}", headers=auth_headers)
    assert r.json()["status"] == "error"
    rows = await _rows(client, kind="command")
    assert len(rows) == 1 and rows[0].action == "docker restart" and "таймаут" in rows[0].message
    assert rows[0].extra["params"]["container"] == "web" and rows[0].section == "docker"
    # повторный опрос той же команды не пишет второй раз
    await client.get(f"/api/servers/{sid}/docker/command/{cid}", headers=auth_headers)
    assert (await _rows(client, kind="command"))[0].count == 1


async def test_front_errors_are_rate_limited(client, auth_headers, monkeypatch):
    monkeypatch.setattr(pe_api, "FRONT_PER_MIN", 5)
    got = []
    for i in range(8):
        r = await client.post("/api/panel-errors/front", headers=auth_headers, json={
            "kind": "error", "message": f"TypeError: x{i} is undefined", "stack": f"at f{i} (index.js:1:{i})",
            "page": "/?section=backups", "section": "backups"})
        assert r.status_code == 200
        got.append(r.json()["recorded"])
    assert got == [True] * 5 + [False] * 3
    rows = await _rows(client, kind="front")
    assert len(rows) == 5 and rows[0].section == "backups" and rows[0].username == "admin"


async def test_report_attaches_recent_errors_of_this_user(client, auth_headers):
    # ошибка человека пару минут назад и чужая ошибка
    await client.delete("/api/checks/424242", headers=auth_headers)
    await pe.drain()
    f = _app(client).state.session_factory
    await pe.record(f, kind="api", action="POST /api/x", status=400, message="чужое", username="someone")
    old = await pe.record(f, kind="api", action="POST /api/old", status=400, message="давно", username="admin")
    from app.models import PanelError

    async with f() as s:
        (await s.get(PanelError, old)).last_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await s.commit()
    r = await client.post("/api/panel-errors/report", headers=auth_headers, json={
        "text": "нажал включить дамп, вылезла ошибка без причины", "page": "/?backup=3&sec=dumps", "section": "backups",
        "client_log": [{"ts": "2026-10-20T10:00:00Z", "method": "POST", "path": "/api/servers/3/backup/command",
                        "status": 400, "detail": "token=abcdef не подошел"}]})
    assert r.status_code == 200, r.text
    assert r.json()["attached"] == 1 and r.json()["client_log"] == 1
    rep = (await _rows(client, kind="report"))[0]
    assert rep.username == "admin" and rep.page == "/?backup=3&sec=dumps" and rep.message.startswith("нажал")
    att = rep.extra["errors"]
    assert [x["action"] for x in att] == ["DELETE /api/checks/{check_id}"]
    assert "abcdef" not in str(rep.extra["client_log"])
    # два одинаковых сообщения не складываются
    await client.post("/api/panel-errors/report", headers=auth_headers, json={"text": "нажал включить дамп, вылезла ошибка без причины"})
    assert len(await _rows(client, kind="report")) == 2


async def test_journal_is_admin_only_but_anyone_can_report(client, auth_headers):
    vh = await _viewer(client, auth_headers)
    assert (await client.get("/api/panel-errors", headers=vh)).status_code == 403
    assert (await client.get("/api/panel-errors/count", headers=vh)).status_code == 403
    assert (await client.post("/api/panel-errors/resolve", headers=vh, json={"ids": [1]})).status_code == 403
    # наблюдатель не меняет объекты, но сообщить о проблеме и прислать ошибку интерфейса может
    assert (await client.post("/api/panel-errors/report", headers=vh, json={"text": "не открывается"})).status_code == 200
    assert (await client.post("/api/panel-errors/front", headers=vh, json={"message": "x"})).status_code == 200
    assert (await client.post("/api/checks", headers=vh, json={})).status_code == 403
    await pe.drain()

    j =(await client.get("/api/panel-errors", headers=auth_headers)).json()
    # отказ наблюдателю на изменение - ожидаемый, его в журнале нет (фронт такие запросы и не шлет)
    assert {x["kind"] for x in j["items"]} == {"report", "front"}
    assert j["counts"]["open"] == 2 and j["counts"]["by_kind"]["report"] == 1
    assert (await client.get("/api/panel-errors/count", headers=auth_headers)).json() == {"open": 2, "reports": 1}
    ids = [x["id"] for x in j["items"]]
    r = await client.post("/api/panel-errors/resolve", headers=auth_headers, json={"ids": ids})
    assert r.json()["changed"] == 2
    assert (await client.get("/api/panel-errors", headers=auth_headers)).json()["items"] == []
    res = (await client.get("/api/panel-errors?state=resolved&q=открывается", headers=auth_headers)).json()["items"]
    assert len(res) == 1 and res[0]["resolved_by"] == "admin"


async def test_viewer_refusal_is_expected_and_plain_text(client, auth_headers):
    vh = await _viewer(client, auth_headers)
    r = await client.post("/api/servers", json={"name": "app-02"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    r = await client.post(f"/api/servers/{sid}/docker/command", headers=vh, json={"container": "web", "action": "restart"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Учетная запись только для просмотра: изменения запрещены"
    # тот же отказ у любого изменения: кнопки у наблюдателя спрятаны, это не сбой панели
    assert (await client.delete(f"/api/servers/{sid}", headers=vh)).status_code == 403
    assert await _rows(client) == []
    # а отказ с другим текстом или кодом - по-прежнему сбой
    assert pe.expected_refusal(403, '{"detail":"Учетная запись только для просмотра: изменения запрещены"}'.encode())
    assert not pe.expected_refusal(403, '{"detail":"Раздел недоступен для этой учётной записи"}'.encode())
    assert not pe.expected_refusal(400, '{"detail":"Учетная запись только для просмотра: изменения запрещены"}'.encode())


async def test_backup_already_there_is_not_a_panel_error(client, auth_headers, monkeypatch):
    from app.api import servers as srv_api
    from app.models import Server

    async def fake(_f, job_id, params):
        return None

    monkeypatch.setattr(srv_api, "_orchestrate_setup", fake)
    a = (await client.post("/api/servers", json={"name": "cl-9"}, headers=auth_headers)).json()["server"]["id"]
    b = (await client.post("/api/servers", json={"name": "bs-9"}, headers=auth_headers)).json()["server"]["id"]
    async with _app(client).state.session_factory() as s:
        ca, cb = await s.get(Server, a), await s.get(Server, b)
        ca.last_report = {"backup": {"present": True, "restic_found": True, "configured": True, "metric_present": True}}
        ca.agent_ip = "10.0.0.5"
        cb.last_report = {"backup_server": {"present": True, "running": True, "port": 64100, "tls_port": 64101}}
        cb.agent_ip = "10.0.0.6"
        await s.commit()
    body = {"backup_server_id": b, "mode": "exclude", "paths": ["/proc"], "schedule": "12:00"}
    # форма открыта по старому списку, а бэкап уже есть: отказ ожидаемый, форма спросит "поверх"
    r = await client.post(f"/api/servers/{a}/backup/setup", json=body, headers=auth_headers)
    assert r.status_code == 409 and "переустановить поверх" in r.json()["detail"]
    assert await _rows(client) == []
    # другой отказ той же формы - в журнале
    r = await client.post(f"/api/servers/{a}/backup/setup", json=body | {"backup_server_id": a, "reinstall": True},
                          headers=auth_headers)
    assert r.status_code == 400
    assert [x.status for x in await _rows(client)] == [400]


async def test_logs_of_a_gone_pod_or_container_are_not_logged(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "kb-9"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    agent = {"Authorization": f"Bearer {token}"}
    gone = ('logs 404: {"kind":"Status","apiVersion":"v1","metadata":{},"status":"Failure",'
            '"message":"pods \\"web-7d9f-abcde\\" not found","reason":"NotFound",'
            '"details":{"name":"web-7d9f-abcde","kind":"pods"},"code":404}')

    async def kube(action, output):
        r = await client.post(f"/api/servers/{sid}/kube/command", headers=auth_headers,
                              json={"ns": "default", "kind": "pod", "name": "web-7d9f-abcde", "action": action})
        assert r.status_code == 200, r.text
        r = await client.post("/api/agent/kube-result", headers=agent, json={"id": r.json()["id"], "ok": False, "output": output})
        assert r.status_code == 200

    # под пересоздали, а человек нажал "логи" в старом списке
    await kube("logs", gone)
    await kube("logs_prev", gone)
    r = await client.post(f"/api/servers/{sid}/docker/command", headers=auth_headers, json={"container": "web", "action": "logs"})
    await client.post("/api/agent/docker-result", headers=agent, json={"id": r.json()["id"], "ok": False, "output": "logs 404"})
    assert await _rows(client) == []
    # другой отказ логов и 404 у другого действия - сбой, в журнале
    await kube("logs", 'logs 403: {"kind":"Status","message":"forbidden","code":403}')
    await kube("delete_pod", "delete 404: not found")
    rows = await _rows(client, kind="command")
    assert sorted(x.action for x in rows) == ["kube delete_pod", "kube logs"]


async def test_prune_keeps_month_and_row_cap(client, monkeypatch):
    f = _app(client).state.session_factory
    ids = [await pe.record(f, kind="api", action=f"POST /api/a{i}", message=f"e{i}") for i in range(5)]
    from app.models import PanelError

    async with f() as s:
        (await s.get(PanelError, ids[0])).last_at = datetime.now(timezone.utc) - timedelta(days=40)
        await s.commit()
    monkeypatch.setattr(pe, "MAX_ROWS", 3)
    async with f() as s:
        await pe.prune(s)
        await s.commit()
    left = [r.id for r in await _rows(client)]
    assert left == ids[2:]


async def test_failed_backup_setup_job_is_logged_with_its_steps(client, auth_headers):
    from app.api.servers import _job_runner
    from app.models import BackupSetupJob

    r = await client.post("/api/servers", json={"name": "bk-client"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    f = _app(client).state.session_factory
    async with f() as s:
        job = BackupSetupJob(server_id=sid, backup_server_id=sid, status="running", steps=[])
        s.add(job)
        await s.commit()
        jid = job.id
    steps, save, _step = _job_runner(f, jid, "admin", "setup")
    steps.append({"step": "provision-repo", "ok": False, "detail": "REPOPASS_B64=<скрыто> init failed"})
    await save("error", "репозиторий не создан")
    rows = await _rows(client, kind="command")
    assert len(rows) == 1 and rows[0].action == "backup setup" and rows[0].server_name == "bk-client"
    assert "FAIL provision-repo" in rows[0].detail and rows[0].extra == {"job_id": jid}
    # без автора (старый вызов) - не пишется
    steps2, save2, _ = _job_runner(f, jid)
    await save2("error", "x")
    assert len(await _rows(client, kind="command")) == 1


async def test_cli_dump_reads_open_errors(client, monkeypatch):
    from app import panel_errors_cli

    f = _app(client).state.session_factory
    await pe.record(f, kind="front", action="interface error", message="TypeError: boom", detail="x" * 3000)
    rid = await pe.record(f, kind="api", action="POST /api/x", message="done")
    from app.models import PanelError

    async with f() as s:
        (await s.get(PanelError, rid)).resolved = True
        await s.commit()
    out = await panel_errors_cli.dump(7, False, 100)
    assert [x["message"] for x in out["items"]] == ["TypeError: boom"]
    assert len(out["items"][0]["detail"]) < 120 and out["version"]
    assert len((await panel_errors_cli.dump(7, True, 100))["items"]) == 2


UI = {"X-Kervax-Page": "/?check=12", "X-Kervax-Section": "sites"}


async def test_invalid_form_from_panel_ui_is_logged_without_values(client, auth_headers):
    # форма панели (api() ставит X-Kervax-Page) прислала то, чего бэкенд не принимает
    for name in (5, 6, 7):
        r = await client.post("/api/checks", headers={**auth_headers, **UI}, json={
            "name": name, "type": "http", "target": "https://example.com", "interval_seconds": f"x{name}",
            "auth_pass": "fakepass" * 40})
        assert r.status_code == 422
        await pe.drain()
    rows = await _rows(client)
    # одна строка на маршрут и набор полей, сколько бы раз ни отправили и что бы ни вписали
    assert len(rows) == 1, [x.message for x in rows]
    r = rows[0]
    assert (r.kind, r.status, r.action, r.count) == ("api", 422, "POST /api/checks", 3)
    assert r.username == "admin" and r.section == "sites" and r.page == "/?check=12"
    assert r.message.startswith("форма не прошла проверку: ") and "interval_seconds" in r.message
    fields = {x["field"] for x in r.extra["invalid"]}
    assert fields == {"name", "interval_seconds", "auth_pass"} and all(set(x) == {"field", "type", "msg"} for x in r.extra["invalid"])
    # значений полей и тела запроса в записи нет
    for where in (r.message, r.detail, str(r.extra)):
        assert "fakepass" not in where and "тело запроса" not in where and "x7" not in where


async def test_invalid_form_other_fields_is_another_row(client, auth_headers):
    await client.post("/api/checks", headers={**auth_headers, **UI}, json={"name": 5})
    await client.post("/api/checks", headers={**auth_headers, **UI}, json={"name": "web", "type": "http", "target": "https://example.com", "interval_seconds": 1})
    assert len(await _rows(client, status=422)) == 2


async def test_invalid_request_from_scripts_is_not_logged(client, auth_headers):
    # без заголовка интерфейса: скрипт или внешний клиент API, его 422 - его ошибка
    assert (await client.post("/api/checks", headers=auth_headers, json={"name": 5})).status_code == 422
    # заголовок есть, а входа нет: 401, не 422
    assert (await client.post("/api/checks", headers=UI, json={"name": 5})).status_code == 401
    # форма входа: у нее свой журнал
    r = await client.post("/api/auth/login", headers=UI, json={"username": 5})
    assert r.status_code == 422
    # адрес страницы - путь, а не что попало
    assert (await client.post("/api/checks", headers={**auth_headers, "X-Kervax-Page": "curl"},
                              json={"name": 5})).status_code == 422
    assert await _rows(client) == []


async def test_invalid_as_plain_text_is_logged(client, auth_headers):
    from fastapi import HTTPException

    async def bad():
        raise HTTPException(422, "Недопустимое правило")

    _app(client).add_api_route("/api/servers/{server_id}/badrule", bad, methods=["PUT"])
    r = await client.put("/api/servers/3/badrule", headers={**auth_headers, **UI}, json={"token": "fake-token-example"})
    assert r.status_code == 422
    (row,) = await _rows(client)
    assert row.message == "форма не прошла проверку: Недопустимое правило"
    assert row.action == "PUT /api/servers/{server_id}/badrule" and row.server_id == 3
    assert "fake-token-example" not in row.detail


def test_invalid_fields_of_cut_response():
    # ответ обрезан на _INVALID_KEPT: JSON не разобрать, поля достаются регуляркой
    raw = (b'{"detail":[{"type":"greater_than_equal","loc":["body","push_period"],"msg":"Input should be greater '
           b'than or equal to 60","input":0,"ctx":{"ge":60}},{"type":"missing","loc":["body","items",0,"name"],'
           b'"msg":"Field required","input":{"password":"fakep')
    fields, text = pe.invalid_fields(raw)
    assert text == "" and [(x["field"], x["type"]) for x in fields] == [
        ("push_period", "greater_than_equal"), ("items.0.name", "missing")]
    assert "fakep" not in str(fields)
    assert pe.invalid_fields(b'{"detail": "no"}') == ([], "no")
    assert pe.invalid_fields(b"") == ([], "")
