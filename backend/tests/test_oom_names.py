"""Имя контейнера и пода в OOM. Ядро называет контейнер только cgroup docker-<id>.scope: до агента
2.46 id контейнеров в отчете не было, и в алерте стоял id ("Контейнер: f4dcb91bdbb0"), хотя
контейнер работал. Контейнер после OOM часто пересоздают с новым id - панель помнит id -> имя из
прошлых отчетов (ct_seen) и пишет имя в строку oom_kills. Под - по ворклоаду без хеша, и в карточке,
и на главной, а не по uid."""
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import oom

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TS = NOW.timestamp()
CID = "f4dc" * 16  # 64 знака, как в cgroup
OLD = "1457" * 16
POD_UID = "e551fc7f-1111-2222-3333-444455556666"
POD_CG = ("/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod"
          + POD_UID.replace("-", "_") + ".slice")


def _ct_kill(cid=CID, dt=300):
    cg = f"/system.slice/docker-{cid}.scope"
    return {"ts": TS - dt, "pid": 1033013, "name": "python", "kind": "memcg", "memcg": cg, "oom_memcg": cg,
            "uid": 0, "user": "root", "anon": 4183544, "mem": {"limit": 4194304},
            "top": [{"name": "python", "uid": 0, "user": "root", "n": 1, "anon": 4183544}]}


def _rep(kills, containers=None, seen=None):
    rep = {"clock_unix": TS, "extras": {"oom": {"v": 1, "ts": TS - 20, "kills": kills, "units": []}}}
    if containers is not None:
        rep["docker"] = {"containers": containers}
    if seen is not None:
        rep["ct_seen"] = seen
    return rep


def _ct(cid, name, project="app", service="worker"):
    return {"id": cid[:12], "name": name, "state": "running",
            "compose": {"project": project, "service": service}}


def _short(rep, pod_names=None):
    e = oom.episodes(oom.records(rep["extras"]["oom"]))[-1]
    return oom.episode_short(e, timezone.utc, set(), oom.names_of(rep, pod_names), pod_names)


def test_container_by_name_from_report_and_memory():
    """Как на проде 21.10: контейнер работает, docker перезапустил его после OOM с тем же id."""
    rep = _rep([_ct_kill()], [_ct(CID, "app-worker-1"), _ct("ab" * 32, "app-api-1", service="api")])
    assert _short(rep) == ("Кончилась память (OOM)", ["Убит: python", "Контейнер: app-worker-1 (лимит 4гб)"])
    # пересоздан через compose с новым id: имя из памяти панели
    seen = oom.remember_containers(None, {"docker": {"containers": [_ct(CID, "app-worker-1")]}}, TS - 600)
    rep2 = _rep([_ct_kill()], [_ct("cd" * 32, "app-worker-1")], seen)
    assert _short(rep2)[1][1] == "Контейнер: app-worker-1 (лимит 4гб)"
    # агент старше 2.46 (id нет) и в памяти пусто: короткий id с пометкой, а не как имя
    assert _short(_rep([_ct_kill()], [{"name": "app-worker-1"}]))[1][1] == \
        "Контейнер: f4dcf4dcf4dc (имя неизвестно, лимит 4гб)"
    # то же в длинном тексте и в виновнике (ключ мьюта по имени, не по id)
    e = oom.episodes(oom.records(rep2["extras"]["oom"]))[-1]
    names = oom.names_of(rep2)
    assert "в лимите контейнер app-worker-1" in oom.episode_text(e, timezone.utc, set(), 1, names)
    assert oom.culprit(e, names) == "ct:app-worker-1" and oom.culprit(e) == "ct:" + CID[:12]
    assert oom.kills_text(e, {}) == "python 4гб (контейнер f4dcf4dcf4dc (имя неизвестно))"


def test_remember_containers_carry_ttl_and_cap():
    a = oom.remember_containers(None, {"docker": {"containers": [_ct(OLD, "app-worker-1")]}}, TS - 86400)
    assert a == {OLD[:12]: {"n": "app-worker-1", "p": "app", "s": "worker", "t": int(TS - 86400)}}
    # следующий отчет без него: помним; новый контейнер того же сервиса - рядом
    b = oom.remember_containers(a, {"docker": {"containers": [_ct(CID, "app-worker-1")]}}, TS)
    assert set(b) == {OLD[:12], CID[:12]} and b[CID[:12]]["t"] == int(TS)
    # дольше CT_KEEP_S не видели - забыли; агент без id - память не трогаем, кроме срока
    late = TS - 86400 + oom.CT_KEEP_S + 60
    assert set(oom.remember_containers(b, {"docker": {"containers": [{"name": "x"}]}}, late)) == {CID[:12]}
    assert oom.remember_containers(None, {}, TS) == {}
    # потолок: остаются самые свежие
    many = {f"{i:012x}": {"n": f"c{i}", "t": int(TS) - i} for i in range(oom.CT_MAX + 50)}
    kept = oom.remember_containers(many, {}, TS)
    assert len(kept) == oom.CT_MAX and f"{0:012x}" in kept and f"{oom.CT_MAX + 49:012x}" not in kept


def test_mute_by_old_id_still_works():
    """До 1.5.2 OOM в контейнере глушили по id (имени панель не знала): такой мьют продолжает действовать."""
    rep = _rep([_ct_kill()], [_ct(CID, "app-worker-1")])
    e = oom.episodes(oom.records(rep["extras"]["oom"]))[-1]
    names = oom.names_of(rep)
    assert oom.episode_muted(e, {"oom:ct:app-worker-1"}, set(), names)
    assert oom.episode_muted(e, {"oom:ct:" + CID[:12]}, set(), names)
    assert not oom.episode_muted(e, {"oom:ct:app-api-1"}, set(), names)


def test_pod_by_workload_in_card_home_and_key():
    kill = {"ts": TS - 300, "pid": 7, "name": "mariadbd", "kind": "memcg", "memcg": POD_CG + "/cri-containerd-ab.scope",
            "oom_memcg": POD_CG, "uid": 999, "user": "", "anon": 1012 * 1024, "mem": {"limit": 1024 * 1024}, "top": []}
    rep = _rep([kill])
    pods = {"e551fc7f": "default/wordpress-mariadb-7d9f8b6c5d-x2k4q"}
    e = oom.episodes(oom.records(rep["extras"]["oom"]))[-1]
    names = oom.names_of(rep, pods)
    # мьют по ворклоаду: новый под того же deployment - тот же виновник; старый мьют по uid действует
    assert oom.culprit(e, names) == "pod:wordpress-mariadb"
    assert oom.culprit_text("pod:wordpress-mariadb") == "под wordpress-mariadb"
    assert oom.culprit_text("pod:" + POD_UID) == "под e551fc7f"
    assert oom.episode_muted(e, {"oom:pod:" + POD_UID}, set(), names)
    v = oom.episode_view(e, names)
    assert v["kills"][0]["owner"] == "wordpress-mariadb" and not v["kills"][0]["owner_unknown"]
    assert v["text"] == "mariadbd 1012мб (под wordpress-mariadb)"
    blind = oom.episode_view(e, {})["kills"][0]
    assert blind["owner"] == "e551fc7f" and blind["owner_unknown"]
    # главная и карточка сервера (view, problem) - тоже по имени
    s = SimpleNamespace(last_report=rep, alert_mutes=[], alert_snoozes={})
    assert oom.view(s, NOW, pods)["last_text"] == "mariadbd 1012мб (под wordpress-mariadb)"
    assert "из-за под wordpress-mariadb" in oom.problem(s, NOW, pods)[1]


def test_names_from_stored_rows():
    """Старый эпизод карточки: контейнера давно нет ни в отчете, ни в памяти, имя - из строки базы."""
    recs = oom.records({"kills": [_ct_kill()]})
    recs[0]["ct"] = "app-worker-1"
    e = oom.episodes(recs)[-1]
    v = oom.episode_view(e, oom.names_of({}, None, recs))
    assert v["kills"][0]["owner"] == "app-worker-1" and v["culprit"] == "ct:app-worker-1"


async def test_report_remembers_names_and_stores_them(client, auth_headers):
    """Через прием отчета: агент 2.46 шлет id, панель помнит контейнер и после того, как его
    пересоздали, пишет имя в строку oom_kills; карточка показывает имя."""
    from app.models import OomKill, Server

    factory = client._transport.app.state.session_factory  # noqa: SLF001
    r = await client.post("/api/servers", json={"name": "web-c"}, headers=auth_headers)
    sid, token = r.json()["server"]["id"], r.json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    now = datetime.now(timezone.utc).timestamp()
    base = {"hostname": "web-c", "uptime_seconds": 5000, "clock_unix": int(now)}
    first = {**base, "docker": {"present": True, "access": True, "containers": [_ct(CID, "app-worker-1")]}}
    assert (await client.post("/api/agent/report", json=first, headers=h)).status_code == 200
    kill = dict(_ct_kill(), ts=now - 300)
    later = {**base, "docker": {"present": True, "access": True, "containers": [_ct("cd" * 32, "app-worker-1")]},
             "extras": {"oom": {"v": 1, "ts": now - 20, "kills": [kill], "units": []}}}
    assert (await client.post("/api/agent/report", json=later, headers=h)).status_code == 200
    async with factory() as s:
        row = (await s.execute(select(OomKill).where(OomKill.server_id == sid))).scalars().one()
        srv = await s.get(Server, sid)
        assert row.data["ct"] == "app-worker-1"
        assert set(srv.last_report["ct_seen"]) == {CID[:12], ("cd" * 32)[:12]}
    eps = (await client.get(f"/api/servers/{sid}/oom", headers=auth_headers)).json()["episodes"]
    assert eps[0]["kills"][0]["owner"] == "app-worker-1" and eps[0]["culprit"] == "ct:app-worker-1"
