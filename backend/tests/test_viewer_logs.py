"""Галка "может читать логи" у учетки только для просмотра: открывает только чтение логов
контейнеров, подов и журнала systemd. Рестарт и прочие изменения через те же эндпоинты - нет."""

import httpx

from app.deps import LOGS_CLOSED, VIEWER_READ_ONLY

PW = "viewerpass-001"


async def _viewer(client: httpx.AsyncClient, auth_headers, name="watcher", **extra) -> tuple[int, dict]:
    r = await client.post("/api/users", json={"username": name, "password": PW, "role": "viewer", **extra},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = await client.post("/api/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _server(client, auth_headers, name="node-a") -> int:
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    return r.json()["server"]["id"]


def _docker(action: str) -> dict:
    return {"container": "web", "action": action}


def _kube(action: str) -> dict:
    return {"ns": "app", "kind": "pod", "name": "web-0", "action": action}


JOURNAL = {"action": "unit_journal", "unit": "nginx.service"}


async def _set_logs(client, auth_headers, uid: int, on: bool) -> dict:
    r = await client.patch(f"/api/users/{uid}", json={"can_read_logs": on}, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _audit(client, auth_headers) -> list[dict]:
    return (await client.get("/api/audit?limit=100", headers=auth_headers)).json()


async def test_viewer_without_flag_gets_no_logs(client, auth_headers):
    sid = await _server(client, auth_headers)
    _, vh = await _viewer(client, auth_headers)
    assert (await client.get("/api/auth/me", headers=vh)).json()["can_read_logs"] is False
    base = f"/api/servers/{sid}"
    for path, body in ((f"{base}/docker/command", _docker("logs")),
                       (f"{base}/kube/command", _kube("logs")),
                       (f"{base}/kube/command", _kube("logs_prev")),
                       (f"{base}/backup/command", JOURNAL)):
        r = await client.post(path, json=body, headers=vh)
        assert r.status_code == 403 and r.json()["detail"] == LOGS_CLOSED, (path, body)
    # рестарт - обычный отказ на изменение
    r = await client.post(f"{base}/docker/command", json=_docker("restart"), headers=vh)
    assert r.status_code == 403 and r.json()["detail"] == VIEWER_READ_ONLY


async def test_viewer_with_flag_reads_logs_only(client, auth_headers):
    sid = await _server(client, auth_headers)
    uid, vh = await _viewer(client, auth_headers)
    out = await _set_logs(client, auth_headers, uid, True)
    assert out["can_read_logs"] is True
    assert (await client.get("/api/auth/me", headers=vh)).json()["can_read_logs"] is True
    base = f"/api/servers/{sid}"

    ids = {}
    for kind, path, body in (("docker", f"{base}/docker/command", _docker("logs")),
                             ("kube", f"{base}/kube/command", _kube("logs")),
                             ("kube_prev", f"{base}/kube/command", _kube("logs_prev")),
                             ("backup", f"{base}/backup/command", JOURNAL)):
        r = await client.post(path, json=body, headers=vh)
        assert r.status_code == 200 and r.json()["status"] == "pending", (path, body, r.text)
        ids[kind] = r.json()["id"]
    # свою команду учетка и опрашивает
    assert (await client.get(f"{base}/docker/command/{ids['docker']}", headers=vh)).status_code == 200
    assert (await client.get(f"{base}/kube/command/{ids['kube']}", headers=vh)).status_code == 200
    assert (await client.get(f"{base}/backup/command/{ids['backup']}", headers=vh)).status_code == 200

    # через те же эндпоинты ничего, кроме чтения
    for path, body in ((f"{base}/docker/command", _docker("restart")),
                       (f"{base}/docker/command", _docker("stop")),
                       (f"{base}/docker/command", _docker("start")),
                       (f"{base}/kube/command", _kube("delete_pod")),
                       (f"{base}/kube/command", {**_kube("rollout_restart"), "kind": "deployment"}),
                       (f"{base}/backup/command", {"action": "unit_fix", "mode": "restart", "unit": "nginx.service"}),
                       (f"{base}/backup/command", {"action": "run_now"}),
                       (f"{base}/backup/command", {"action": "disk_fix", "mode": "run", "fix": "analyze", "mount": "/"})):
        r = await client.post(path, json=body, headers=vh)
        assert r.status_code == 403 and r.json()["detail"] == VIEWER_READ_ONLY, (path, body)
    # непонятное тело - не логи (битый JSON FastAPI отбивает еще до проверки учетки)
    r = await client.post(f"{base}/docker/command", content=b"logs", headers={**vh, "Content-Type": "application/json"})
    assert r.status_code in (403, 422)
    r = await client.post(f"{base}/docker/command", json=["logs"], headers=vh)
    assert r.status_code == 403
    # другие изменения тоже закрыты
    assert (await client.post(f"{base}/snooze", json={"minutes": 60}, headers=vh)).status_code == 403
    assert (await client.patch(f"{base}", json={"note": "x"}, headers=vh)).status_code == 403

    # в журнале: кто включил галку, кто и на каком сервере смотрел логи
    log = await _audit(client, auth_headers)
    on = [x for x in log if x["action"] == "user_logs_on"]
    assert on and on[0]["username"] == "admin" and on[0]["target"] == "watcher"
    reads = {x["action"]: x for x in log if x["username"] == "watcher"}
    assert reads["docker_logs"]["target"] == "web" and reads["docker_logs"]["detail"] == "node-a"
    assert reads["kube_logs"]["target"] == "app/pod/web-0" and reads["kube_logs_prev"]["detail"] == "node-a"
    assert reads["backup_unit_journal"]["target"].startswith("nginx.service:journal")
    assert not any(x["username"] == "watcher" and x["action"] in ("docker_restart", "kube_delete_pod")
                   for x in log)


async def test_viewer_cannot_read_others_command_results(client, auth_headers):
    """Лог, который запросил админ, учетке только для просмотра по номеру команды не прочитать."""
    sid = await _server(client, auth_headers)
    uid, vh = await _viewer(client, auth_headers)
    base = f"/api/servers/{sid}"
    adm = (await client.post(f"{base}/docker/command", json=_docker("logs"), headers=auth_headers)).json()["id"]
    assert (await client.get(f"{base}/docker/command/{adm}", headers=vh)).status_code == 404
    await _set_logs(client, auth_headers, uid, True)
    assert (await client.get(f"{base}/docker/command/{adm}", headers=vh)).status_code == 404
    mine = (await client.post(f"{base}/docker/command", json=_docker("logs"), headers=vh)).json()["id"]
    assert (await client.get(f"{base}/docker/command/{mine}", headers=vh)).status_code == 200
    # галку сняли - и свое уже не прочитать
    await _set_logs(client, auth_headers, uid, False)
    assert (await client.get(f"{base}/docker/command/{mine}", headers=vh)).status_code == 404
    r = await client.post(f"{base}/docker/command", json=_docker("logs"), headers=vh)
    assert r.status_code == 403 and r.json()["detail"] == LOGS_CLOSED
    off = [x for x in await _audit(client, auth_headers) if x["action"] == "user_logs_off"]
    assert off and off[0]["username"] == "admin" and off[0]["target"] == "watcher"
    # админ свое и чужое видит, как раньше
    assert (await client.get(f"{base}/docker/command/{mine}", headers=auth_headers)).status_code == 200


async def test_admin_and_editor_as_before(client, auth_headers):
    sid = await _server(client, auth_headers)
    base = f"/api/servers/{sid}"
    for body in (_docker("logs"), _docker("restart")):
        assert (await client.post(f"{base}/docker/command", json=body, headers=auth_headers)).status_code == 200
    for body in (_kube("logs"), _kube("delete_pod")):
        assert (await client.post(f"{base}/kube/command", json=body, headers=auth_headers)).status_code == 200
    assert (await client.post(f"{base}/backup/command", json=JOURNAL, headers=auth_headers)).status_code == 200
    r = await client.post("/api/users", json={"username": "ed", "password": PW, "role": "editor"}, headers=auth_headers)
    assert r.status_code == 201
    eh = {"Authorization": "Bearer " + (await client.post(
        "/api/auth/login", json={"username": "ed", "password": PW})).json()["access_token"]}
    assert (await client.post(f"{base}/docker/command", json=_docker("restart"), headers=eh)).status_code == 200
    assert (await client.post(f"{base}/docker/command", json=_docker("logs"), headers=eh)).status_code == 200


async def test_flag_only_for_viewer(client, auth_headers):
    """Галка живет только у учетки только для просмотра: другой роли она не нужна, а при смене
    роли снимается, чтобы при возврате к просмотру логи не открылись сами."""
    r = await client.post("/api/users", json={"username": "ed", "password": PW, "role": "editor",
                                              "can_read_logs": True}, headers=auth_headers)
    assert r.status_code == 201 and r.json()["can_read_logs"] is False
    uid, _ = await _viewer(client, auth_headers, can_read_logs=True)
    users = {u["username"]: u for u in (await client.get("/api/users", headers=auth_headers)).json()}
    assert users["watcher"]["can_read_logs"] is True
    assert (await client.patch(f"/api/users/{uid}", json={"role": "editor"}, headers=auth_headers)).json()["can_read_logs"] is False
    assert (await client.patch(f"/api/users/{uid}", json={"role": "viewer"}, headers=auth_headers)).json()["can_read_logs"] is False
    acts = [x["action"] for x in await _audit(client, auth_headers) if x["target"] == "watcher"]
    assert "user_logs_on" in acts and "user_logs_off" in acts
    # саму себе галку учетка не поставит
    _, vh = await _viewer(client, auth_headers, name="other")
    assert (await client.patch(f"/api/users/{uid}", json={"can_read_logs": True}, headers=vh)).status_code == 403


async def test_impersonation_still_read_only(client, auth_headers):
    """Админ под учеткой с галкой смотрит глазами пользователя: команды, и логи тоже, не уходят."""
    sid = await _server(client, auth_headers)
    uid, _ = await _viewer(client, auth_headers, can_read_logs=True)
    r = await client.post(f"/api/users/{uid}/impersonate", headers=auth_headers)
    assert r.status_code == 200, r.text
    imp = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post(f"/api/servers/{sid}/docker/command", json=_docker("logs"), headers=imp)
    assert r.status_code == 403
