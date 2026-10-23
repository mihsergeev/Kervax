"""Форма правки монитора шлет обратно все, что пришло в карточке (checkToForm во фронте).
Схема правки обязана это принимать: иначе "Сохранить" дает 422 на любом поле, которое
человек даже не видел. Так было с push_period=0 у всех мониторов, кроме push."""
import httpx
import pytest

from app import checks as checks_exec
from app.models import Check
from app.schemas import CheckCreate, CheckUpdate


@pytest.fixture
def _fake_runner(monkeypatch):
    async def fake(check):
        return checks_exec.CheckOutcome("up", latency_ms=42, value=None, message="ok")

    monkeypatch.setattr("app.checks.run_check", fake)


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


KINDS = [
    {"name": "сайт @someone", "type": "http", "target": "https://example.com"},
    {"name": "порт", "type": "tcp_port", "target": "example.com", "port": 443},
    {"name": "dns", "type": "dns", "target": "example.com", "dns_type": "A"},
    {"name": "крон", "type": "push", "target": "push", "push_period": 3600},
]


@pytest.mark.parametrize("body", KINDS, ids=[k["type"] for k in KINDS])
async def test_edit_form_roundtrip(client: httpx.AsyncClient, auth_headers, _fake_runner, body):
    r = await client.post("/api/checks", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    if body["type"] != "push":
        # так лежат мониторы, заведенные до push: период 0
        async with _factory(client)() as s:
            c = await s.get(Check, cid)
            c.push_period = 0
            await s.commit()
    got = (await client.get(f"/api/checks/{cid}", headers=auth_headers)).json()
    form = {k: got[k] for k in CheckUpdate.model_fields if k in got}
    # человек убрал из названия упоминание и сохранил
    form["name"] = "без упоминания"
    r = await client.patch(f"/api/checks/{cid}", json=form, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "без упоминания"
    if body["type"] == "push":
        assert r.json()["push_period"] == 3600


async def test_push_without_period_gets_hour(client: httpx.AsyncClient, auth_headers):
    body = {"name": "крон", "type": "push", "target": "push", "push_period": 0}
    r = await client.post("/api/checks", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    assert r.json()["push_period"] == 3600
    r = await client.patch(f"/api/checks/{r.json()['id']}", json={"push_period": 30}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["push_period"] == 60


def test_create_accepts_exported_monitor():
    # выгрузка мониторов несет push_period=0 у сайтов: импорт (CheckCreate) должен ее принять
    CheckCreate(name="сайт", type="http", target="https://example.com", push_period=0)
