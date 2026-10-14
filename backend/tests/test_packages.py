"""Пакеты на серверах (app/packages.py, агент 2.28): список кусками, журнал установок в хронологии,
поиск. Только данные: ни алертов, ни пунктов "требует действий"."""
import gzip
import json
import time

from sqlalchemy import select

from app import packages, search as search_mod, timeline
from app.models import Server, ServerEvent, ServerPackages


async def _enroll(client, auth_headers, name):
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201
    return r.json()["server"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


async def _report(client, h, **kw):
    r = await client.post("/api/agent/report", json={"hostname": "h", "agent_version": "2.28", **kw}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _upload(client, h, hsh, parts):
    done = None
    # куски могут прийти в любом порядке, собирается по последнему недостающему
    for i in reversed(range(len(parts))):
        body = json.dumps({"h": hsh, "mgr": "dpkg", "part": i, "parts": len(parts), "items": parts[i]}).encode()
        r = await client.post("/api/agent/packages", content=gzip.compress(body),
                              headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"})
        assert r.status_code == 200, r.text
        done = r.json()["done"]
    return done


async def test_packages_upload_and_search(client, auth_headers):
    search_mod._cache.clear()
    search_mod._pkg_items.clear()
    search_mod._pkg_index.clear()
    sid, h = await _enroll(client, auth_headers, "web-a")
    sid_b, h_b = await _enroll(client, auth_headers, "web-b")

    # отпечаток новый - панель просит список
    assert (await _report(client, h, pkgs={"mgr": "dpkg", "n": 3, "h": "aaa", "log": True}))["want_pkgs"] is True
    assert await _upload(client, h, "aaa", [[["openssl", "3.0.2-0ubuntu1.15"], ["libc6", "2.35-0ubuntu3.8"]],
                                            [["nginx", "1.18.0-6ubuntu14"]]]) is True
    assert (await _report(client, h, pkgs={"mgr": "dpkg", "n": 3, "h": "aaa", "log": True}))["want_pkgs"] is False
    # агент без пакетов (старый) - ничего не просим
    assert (await _report(client, h_b))["want_pkgs"] is False

    j = (await client.get(f"/api/servers/{sid}/packages", params={"q": "ssl"}, headers=auth_headers)).json()
    assert j["items"] == [["openssl", "3.0.2-0ubuntu1.15"]] and j["n"] == 3 and j["mgr"] == "dpkg"

    # чужой токен и кусок за пределами - мимо
    bad = {"h": "x", "part": 0, "parts": 1, "items": []}
    assert (await client.post("/api/agent/packages", json=bad, headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.post("/api/agent/packages", json={**bad, "part": 1}, headers=h)).status_code == 400

    # второй сервер: та же версия openssl и более новая nginx
    assert (await _report(client, h_b, pkgs={"mgr": "dpkg", "n": 2, "h": "bbb"}))["want_pkgs"] is True
    await _upload(client, h_b, "bbb", [[["openssl", "3.0.2-0ubuntu1.15"], ["nginx", "1.24.0-2ubuntu7"]]])

    async def q(text):
        r = await client.get("/api/search", params={"q": text}, headers=auth_headers)
        return {g["kind"]: g for g in r.json()["groups"]}.get("package")

    g = await q("openssl")
    assert g["total"] == 1
    it = g["items"][0]
    assert it["sub"] == ["3.0.2-0ubuntu1.15", "2", "web-a", "web-b"] and it["sec"] == "pkgs"
    # слово из версии сужает: nginx 1.18 - только на web-a
    g = await q("nginx 1.18")
    assert g["total"] == 1 and g["items"][0]["id"] == sid and g["items"][0]["sub"][1] == "1"
    assert (await q("nginx"))["total"] == 2

    # список сменился - поиск видит новый
    await _report(client, h, pkgs={"mgr": "dpkg", "n": 1, "h": "ccc"})
    await _upload(client, h, "ccc", [[["openssl", "3.0.2-0ubuntu1.18"]]])
    vers = sorted(x["sub"][0] for x in (await q("openssl"))["items"])
    assert vers == ["3.0.2-0ubuntu1.15", "3.0.2-0ubuntu1.18"]

    # удаление сервера уносит и список
    assert (await client.delete(f"/api/servers/{sid}", headers=auth_headers)).status_code == 204
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        assert await s.get(ServerPackages, sid) is None


async def test_package_log_goes_to_timeline(client, auth_headers):
    sid, h = await _enroll(client, auth_headers, "web-a")
    t0 = int(time.time()) - 3 * 3600
    run1 = [
        {"t": t0, "a": "upgrade", "p": "openssl", "o": "3.0.2-0ubuntu1.15", "n": "3.0.2-0ubuntu1.18"},
        {"t": t0 + 20, "a": "upgrade", "p": "libc6", "o": "2.35-0ubuntu3.7", "n": "2.35-0ubuntu3.8"},
        {"t": t0 + 40, "a": "install", "p": "foo", "o": "", "n": "1.0"},
    ]
    run2 = [{"t": t0 + 7200, "a": "remove", "p": "bar", "o": "4.5", "n": ""}]
    await _report(client, h, pkgs={"mgr": "dpkg", "n": 10, "h": "aaa", "log": True}, pkg_ev=run1 + run2)
    # агент перезапустился и прислал журнал заново - повторов нет
    await _report(client, h, pkg_ev=run1 + run2)
    # продолжение прогона в следующем отчете склеивается с ним
    await _report(client, h, pkg_ev=[{"t": t0 + 7260, "a": "purge", "p": "bar", "o": "", "n": ""},
                                     {"t": t0 + 7270, "a": "remove", "p": "baz", "o": "1", "n": ""}])

    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        rows = list(await s.scalars(select(ServerEvent).where(ServerEvent.server_id == sid, ServerEvent.kind == "pkgs")
                                    .order_by(ServerEvent.ts)))
        srv = await s.get(Server, sid)
        assert "pkg_ev" not in (srv.last_report or {})
    assert [e.n for e in rows] == [3, 2]
    assert rows[0].text == ("пакеты: обновлены (2): openssl 3.0.2-0ubuntu1.15 -> 3.0.2-0ubuntu1.18, "
                            "libc6 2.35-0ubuntu3.7 -> 2.35-0ubuntu3.8; установлены (1): foo 1.0")
    assert rows[1].text == "пакеты: удалены (2): bar 4.5, baz 1"

    j = (await client.get(f"/api/servers/{sid}/timeline?days=1", headers=auth_headers)).json()
    ev = [e for e in j["events"] if e["kind"] == "pkgs"]
    assert len(ev) == 2 and len(ev[-1]["data"]) == 3


def test_merge_one_line_per_package():
    m = packages._merge(
        [{"a": "upgrade", "p": "a", "o": "1", "n": "2"}, {"a": "install", "p": "b", "o": "", "n": "1"}],
        [{"a": "upgrade", "p": "a", "o": "2", "n": "3"}, {"a": "upgrade", "p": "b", "o": "1", "n": "2"},
         {"a": "upgrade", "p": "c", "o": "1", "n": "2"}, {"a": "remove", "p": "c", "o": "2", "n": ""}])
    assert m == [{"a": "upgrade", "p": "a", "o": "1", "n": "3"}, {"a": "install", "p": "b", "o": "", "n": "2"},
                 {"a": "remove", "p": "c", "o": "1", "n": ""}]
    assert packages._text([{"a": "upgrade", "p": f"p{i}", "o": "1", "n": "2"} for i in range(6)]) == \
        "пакеты: обновлены (6): p0, p1, p2, p3 и еще 2"


def test_counter_event_skipped_when_log_exists():
    base = {"ts": "t", "c": {}, "p": {}, "boot": "", "k": "", "kn": "", "os": "", "dk": "", "ag": "",
            "rb_ts": 0, "rb_pkgs": [], "ua": 0}
    prev = {**base, "upd": 40, "upd_ts": 1000}
    # с журналом установок счетчик "обновлено N" не нужен: обновления видны по пакетам
    ev, _ = timeline.diff(prev, {**base, "upd": 3, "upd_ts": 4600, "pkh": True}, {})
    assert "packages" not in [e["kind"] for e in ev]
    ev, _ = timeline.diff(prev, {**base, "upd": 3, "upd_ts": 4600}, {})
    assert "packages" in [e["kind"] for e in ev]
