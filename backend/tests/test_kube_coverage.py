"""Ноды кластера без агента панели (kube_coverage) и "агент не нужен".

Повод - k8s-a-prc: воркер кластера без агента, на котором неделями крутились 49 зависших
chrome и жгли 45 ядер из 96. Панель видела кластер через контроллер, а сами воркеры - нет.
"""

import httpx

from app import kube_coverage
from app.models import Server


def _srv(name, kube=None, hostname=None, local="", external="", agent="", ignored=None):
    rep = {"hostname": hostname or name}
    if kube is not None:
        rep["kube"] = kube
    return Server(name=name, token_hash="x", last_report=rep, local_ip=local,
                  external_ip=external, agent_ip=agent, kube_node_ignored=ignored)


def _kube(*nodes):
    return {"present": True, "access": True, "nodes": [{"name": n, "ip": ip, "ready": True} for n, ip in nodes]}


def test_workers_without_agent_are_found():
    # контроллер k0s без воркера: сам в списке нод не значится, три воркера - без агента
    mn3 = _srv("node-35", _kube(("k8s-a-prc", "192.168.77.43"), ("k8s-a-wn1", "192.168.77.41"),
                                    ("k8s-a-wn2", "192.168.78.42")), local="203.0.113.53")
    ch4 = _srv("node-34", local="10.0.0.4")
    idx = kube_coverage.build_index([mn3, ch4])
    got = kube_coverage.unmonitored(mn3, idx)
    assert [n["name"] for n in got] == ["k8s-a-prc", "k8s-a-wn1", "k8s-a-wn2"]
    # внутренний адрес в "IP сервера" не подставляем: для списка разрешенных он бесполезен
    assert got[0] == {"name": "k8s-a-prc", "ip": "192.168.77.43", "public": False}


def test_node_matched_by_name_hostname_or_address():
    k = _kube(("app-b", "203.0.113.51"),      # имя совпадает с сервером
              ("worker-a.example.com", "10.1.0.5"),     # hostname агента короткий
              ("node-x", "203.0.113.7"),                # совпал внешний адрес
              ("node-y", "10.1.0.9"),                   # совпал local_ip
              ("ghost", "93.184.216.34"))               # никого
    ctl = _srv("app-b", k, local="203.0.113.51")
    a = _srv("Worker A", hostname="worker-a")
    x = _srv("srv-x", external="203.0.113.7")
    y = _srv("srv-y", local="10.1.0.9")
    idx = kube_coverage.build_index([ctl, a, x, y])
    assert kube_coverage.unmonitored(ctl, idx) == [{"name": "ghost", "ip": "93.184.216.34", "public": True}]


def test_single_node_cluster_and_no_access_are_quiet():
    one = _srv("k8s-d", _kube(("k8s-d", "203.0.113.52")), local="203.0.113.52")
    idx = kube_coverage.build_index([one])
    assert kube_coverage.unmonitored(one, idx) == []
    # без доступа к kube-api списка нод нет - и подсказки тоже
    noacc = _srv("c", {"present": True, "access": False, "nodes": [{"name": "w1", "ip": "1.2.3.4"}]})
    assert kube_coverage.unmonitored(noacc, kube_coverage.build_index([noacc])) == []
    assert kube_coverage.unmonitored(_srv("plain"), kube_coverage.build_index([])) == []


def test_ignored_nodes_are_skipped():
    ctl = _srv("ctl", _kube(("w1", "10.0.0.1"), ("W2", "10.0.0.2")), ignored=["w2"])
    assert [n["name"] for n in kube_coverage.unmonitored(ctl, kube_coverage.build_index([ctl]))] == ["w1"]


async def _cluster(client, auth_headers, name="a-mn3", group=None, nodes=("a-prc", "a-wn1")):
    body = {"name": name} | ({"group_name": group} if group else {})
    r = await client.post("/api/servers", json=body, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    report = {
        "hostname": name, "os": "Ubuntu 24.04", "agent_version": "2.20",
        "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
        "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
        "kube": {"present": True, "access": True, "flavor": "k0s",
                 "nodes": [{"name": n, "ip": f"192.168.77.{i + 40}", "ready": True} for i, n in enumerate(nodes)]},
    }
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return sid


async def test_list_and_ignore_api(client: httpx.AsyncClient, auth_headers):
    sid = await _cluster(client, auth_headers)
    srv = next(s for s in (await client.get("/api/servers", headers=auth_headers)).json() if s["id"] == sid)
    assert [n["name"] for n in srv["kube_unmonitored"]] == ["a-prc", "a-wn1"]

    # агент на a-wn1 поставили: нода ушла из подсказки сама
    await client.post("/api/servers", json={"name": "a-wn1"}, headers=auth_headers)
    srv = next(s for s in (await client.get("/api/servers", headers=auth_headers)).json() if s["id"] == sid)
    assert [n["name"] for n in srv["kube_unmonitored"]] == ["a-prc"]

    # на a-prc агент не нужен - скрыли, потом вернули
    r = await client.post(f"/api/servers/{sid}/kube/node-ignore", headers=auth_headers,
                          json={"node": "a-prc", "ignored": True})
    assert r.status_code == 200, r.text
    assert r.json()["kube_unmonitored"] == [] and r.json()["kube_node_ignored"] == ["a-prc"]
    r = await client.post(f"/api/servers/{sid}/kube/node-ignore", headers=auth_headers,
                          json={"node": "a-prc", "ignored": False})
    assert [n["name"] for n in r.json()["kube_unmonitored"]] == ["a-prc"]

    # мусор в имени ноды не принимаем
    r = await client.post(f"/api/servers/{sid}/kube/node-ignore", headers=auth_headers,
                          json={"node": "bad name;rm -rf", "ignored": True})
    assert r.status_code == 422


async def test_scoped_user_sees_nodes_watched_by_other_groups(client: httpx.AsyncClient, auth_headers):
    # учетка видит только группу "op", а a-prc смотрит сервер из группы "infra": нода под
    # присмотром, подсвечивать ее как "без агента" нельзя
    sid = await _cluster(client, auth_headers, group="op", nodes=("a-prc",))
    await client.post("/api/servers", json={"name": "a-prc", "group_name": "infra"}, headers=auth_headers)
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "opview", "password": "Str0ng-pass-123!", "role": "viewer", "server_groups": ["op"],
    })
    assert r.status_code in (200, 201), r.text
    r = await client.post("/api/auth/login", json={"username": "opview", "password": "Str0ng-pass-123!"})
    assert r.status_code == 200, r.text
    hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
    lst = (await client.get("/api/servers", headers=hdr)).json()
    assert [s["id"] for s in lst] == [sid]
    assert lst[0]["kube_unmonitored"] == []
