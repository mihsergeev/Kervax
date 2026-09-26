"""Доступ ansible к списку нод.

Helper'ы на нодах выкатывает плейбук kervax_helpers.yml. Хосты для него раньше
копировались из "Требует действий" каждой из трех панелей, а запуск без -l пошел бы
по всему инвентарю. Теперь плагин инвентаря спрашивает панели сам - по отдельному
токену только на чтение.
"""

from tests.test_users import _mk_viewer


async def _report(client, token: str, **extra) -> None:
    body = {"hostname": "ru-cs24-vdnhado", "os": "Ubuntu", "agent_version": "2.10",
            "cpu_percent": 5, "mem_used": 1, "mem_total": 2, **extra}
    r = await client.post("/api/agent/report", json=body,
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


async def test_token_is_shown_once_and_lists_nodes_to_update(client, auth_headers, monkeypatch):
    # раздается timesync-setup 0.3, на ноде стоит 0.2 - плейбук её обновит
    for where in ("app.api.ansible.current_setup_versions",
                  "app.api.servers._current_setup_versions"):
        monkeypatch.setattr(where, lambda: {"timesync-setup": "0.3"})
    r = await client.post("/api/servers", json={"name": "ru-cs24-vdnh-ado"}, headers=auth_headers)
    old = r.json()["token"]
    await _report(client, old, setup_versions={"timesync-setup": "0.2"})
    r = await client.post("/api/servers", json={"name": "fresh"}, headers=auth_headers)
    fresh = r.json()["token"]
    await _report(client, fresh, hostname="fresh", setup_versions={"timesync-setup": "0.3"})
    # агент ни разу не прислал отчет - в инвентарь не идет
    await client.post("/api/servers", json={"name": "never-reported"}, headers=auth_headers)

    r = await client.get("/api/settings/ansible", headers=auth_headers)
    assert r.json()["enabled"] is False
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    assert r.status_code == 200
    token = r.json()["token"]
    assert len(token) >= 40

    r = await client.get("/api/ansible/servers", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    rows = {x["name"]: x for x in r.json()["servers"]}
    assert set(rows) == {"ru-cs24-vdnh-ado", "fresh"}
    # имя в панели и hostname расходятся - отдаем оба, инвентарь найдет по любому
    assert rows["ru-cs24-vdnh-ado"]["hostname"] == "ru-cs24-vdnhado"
    assert rows["ru-cs24-vdnh-ado"]["rollout"] is True
    assert rows["ru-cs24-vdnh-ado"]["outdated"] == ["timesync-setup"]
    assert rows["fresh"]["rollout"] is False

    # в настройках видно, что связка работает
    r = await client.get("/api/settings/ansible", headers=auth_headers)
    assert r.json()["enabled"] is True and r.json()["used_at"]
    # сам токен из настроек больше не достать - в базе только хеш
    assert token not in r.text

    # тот же признак идет на главную: список там и в ansible не разойдется
    r = await client.get("/api/servers", headers=auth_headers)
    flags = {x["name"]: x["helper_rollout"] for x in r.json()}
    assert flags["ru-cs24-vdnh-ado"] is True and flags["fresh"] is False


async def test_wrong_or_revoked_token_is_refused(client, auth_headers):
    r = await client.get("/api/ansible/servers")
    assert r.status_code == 401  # доступ не включен
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    token = r.json()["token"]
    r = await client.get("/api/ansible/servers", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    # новый токен заменяет старый
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    newer = r.json()["token"]
    r = await client.get("/api/ansible/servers", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    r = await client.get("/api/ansible/servers", headers={"Authorization": f"Bearer {newer}"})
    assert r.status_code == 200
    r = await client.delete("/api/settings/ansible", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get("/api/ansible/servers", headers={"Authorization": f"Bearer {newer}"})
    assert r.status_code == 401


async def test_only_admin_manages_the_token(client, auth_headers):
    viewer = await _mk_viewer(client, auth_headers)
    assert (await client.get("/api/settings/ansible", headers=viewer)).status_code == 403
    assert (await client.post("/api/settings/ansible", headers=viewer)).status_code == 403
    # и JWT обычной учетки ansible-ручку не открывает: там только свой токен
    assert (await client.get("/api/ansible/servers", headers=auth_headers)).status_code == 401


def test_webserver_helper_is_needed_where_it_already_counts_requests():
    """ru-se-mshost: агент веб-сервер не распознал (web_services пуст), а helper 0.7 слал
    блок запросов - устаревший helper там не флагался ни разу."""
    from app.setup_scripts import setup_needed

    assert setup_needed("webserver-setup", {"web_services": [{"kind": "nginx"}]})
    assert setup_needed("webserver-setup", {"extras": {"web-rate": {"ts": 1, "rpm": 0, "logs": []}}})
    # без веб-сервера и без блока - не нужен: доменов там все равно нет
    assert not setup_needed("webserver-setup", {"extras": {"custom-backups": {}}})
