"""Карточка "Похоже на белый список": только устойчивая блокировка снаружи у живого сайта.

Поймано на живом парке: сервер принял запрос и завис на 10 с, изнутри ноды сайт тоже не
открылся, а карточка советовала проверять его изнутри - как будто там белый список. Такое
было со всеми сайтами парка, что ложились хоть на пару минут.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import allowlist_hint as ah
from app.models import Check, CheckIncident, CheckSample, Location, LocationResult, Server

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def test_outside_kind():
    # принял запрос и молчит: соединение пустили, файрвол тут ни при чем
    assert ah.outside_kind("сервер принял запрос, но не ответил за 10 с", {"fail": "wait"}, 0) == ""
    assert ah.outside_kind("сервер принял запрос, но не прислал ответ вовремя (ReadTimeout)", None, 0) == ""
    assert ah.outside_kind("соединение есть, но TLS-рукопожатие висит: 10 с без ответа", {"fail": "tls"}, 0) == ""
    assert ah.outside_kind("плохой ответ от бэкенда (HTTP 502)", None, 0) == ""
    assert ah.outside_kind("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"}, 0) == "connect"
    assert ah.outside_kind("сервер не ответил на попытку соединения (ConnectTimeout)", None, 0) == "connect"
    assert ah.outside_kind("сервер оборвал соединение (Connection reset by peer)", None, 0) == "reset"
    assert ah.outside_kind("доступ запрещён (HTTP 403)", None, 403) == "code"


def _judge(**kw):
    base = dict(outside="connect", since=NOW - timedelta(hours=5), now=NOW, locations=["down", "down"],
                inside="up", ever_up=True, up7=60.0, up30=85.0)
    base.update(kw)
    return ah.judge(**base)


def test_judge():
    hint = _judge()
    assert hint is not None and hint.reason == "inside_up"
    # снаружи ни разу не открывался, изнутри ответа нет - тоже похоже
    assert _judge(inside="", ever_up=False, up7=0.0, up30=0.0).reason == "never_up"
    assert _judge(outside="code", code=403, inside="", ever_up=False).reason == "code"
    # прокси не пускает и изнутри: для 403 это не авария
    assert _judge(outside="code", code=403, inside="down", ever_up=False).reason == "code"

    assert _judge(outside="") is None                                 # ожидание ответа, 5xx
    assert _judge(since=NOW - timedelta(minutes=30)) is None          # короткий инцидент
    assert _judge(locations=["down", "up"]) is None                   # из одной точки открывается
    assert _judge(inside="down") is None                              # изнутри тоже лежит
    assert _judge(up7=99.0, up30=99.5) is None                        # обычно доступен снаружи
    assert _judge(inside="", ever_up=True) is None                    # раньше открывался, изнутри не знаем


def test_inside_status_only_from_this_incident():
    since = NOW - timedelta(hours=3)
    assert ah.inside_status({"ts": (since + timedelta(seconds=40)).isoformat(), "status": "up"}, since) == "up"
    assert ah.inside_status({"ts": (since + timedelta(seconds=40)).isoformat(), "status": "down"}, since) == "down"
    # ответ из прошлого сбоя к этому не относится
    assert ah.inside_status({"ts": (since - timedelta(days=1)).isoformat(), "status": "up"}, since) == ""
    assert ah.inside_status({"asked": since.isoformat()}, since) == ""


async def _site(client, auth_headers, host: str) -> int:
    r = await client.post("/api/checks", headers=auth_headers,
                          json={"name": host, "type": "http", "target": f"https://{host}"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _hints(client, auth_headers) -> dict[int, dict]:
    r = await client.get("/api/checks/local-probe-suggestions", headers=auth_headers)
    assert r.status_code == 200, r.text
    return {x["check_id"]: x for x in r.json()["items"]}


async def test_suggestions_on_real_states(client, auth_headers):
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    hosts = ["hang.example.com", "closed.example.com", "short.example.com",
             "dead.example.com", "usual.example.com", "seen.example.com"]
    ids = {h: await _site(client, auth_headers, h) for h in hosts}
    since = now - timedelta(hours=4)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="a", enabled=True,
                     last_report={"web_services": [{"kind": "nginx", "sites": hosts}]}))
        loc = Location(name="loc-1", url="http://proxy.example.com:3128", enabled=True)
        s.add(loc)
        await s.flush()
        inside_up = {"ts": (since + timedelta(minutes=1)).isoformat(), "status": "up",
                     "server": "web-a", "message": "HTTP 200"}
        state = {
            # сервер принял запрос и висит: не белый список, даже если изнутри открылось
            "hang.example.com": ("сервер принял запрос, но не ответил за 10 с", {"fail": "wait"}, inside_up, since),
            # режут на подключении часами, изнутри открывается - белый список
            "closed.example.com": ("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"}, inside_up, since),
            # то же, но полчаса - авария, а не правило
            "short.example.com": ("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"},
                                  inside_up, now - timedelta(minutes=30)),
            # изнутри тоже не открылось - сайт лежит сам
            "dead.example.com": ("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"},
                                 {**inside_up, "status": "down"}, since),
            # обычно доступный сайт: неделю был зеленым
            "usual.example.com": ("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"}, inside_up, since),
            # из точки проверки открывается
            "seen.example.com": ("сервер не принимает соединение: 10 с без ответа", {"fail": "connect"}, inside_up, since),
        }
        for host, (msg, phases, inside, started) in state.items():
            row = (await s.scalars(select(Check).where(Check.id == ids[host]))).one()
            row.last_status, row.last_message, row.last_phases, row.last_inside = "down", msg, phases, inside
            row.check_locations = True
            s.add(CheckIncident(check_id=row.id, status="down", started_at=started))
            s.add(LocationResult(check_id=row.id, location_id=loc.id,
                                 status="up" if host == "seen.example.com" else "down"))
            # раньше сайт открывался, но редко: аптайм низкий
            for i in range(10):
                up = host == "usual.example.com" or i < 3
                s.add(CheckSample(check_id=row.id, ts=now - timedelta(days=1, minutes=i),
                                  status="up" if up else "down"))
        await s.commit()

    got = await _hints(client, auth_headers)
    assert set(got) == {ids["closed.example.com"]}, got
    hint = got[ids["closed.example.com"]]
    assert hint["kind"] == "local" and hint["reason"] == "inside_up"
    assert hint["outside"] == "connect" and hint["server_name"] == "web-a" and hint["since"]

    # сайт поднялся - из карточки уходит сам, ничего не нажимая
    async with factory() as s:
        row = (await s.scalars(select(Check).where(Check.id == ids["closed.example.com"]))).one()
        row.last_status = "up"
        await s.commit()
    assert await _hints(client, auth_headers) == {}
