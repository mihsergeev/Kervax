"""Агент 2.74 сначала пробует новую версию: цель обновления держится, пока проба не принята."""

from app.models import Server
from tests.test_manual_run import REPORT


async def test_target_survives_agent_trial(client, auth_headers):
    r = await client.post("/api/servers", headers=auth_headers, json={"name": "node-trial"})
    sid = r.json()["server"]["id"]
    agent = {"Authorization": f"Bearer {r.json()['token']}"}
    factory = client._transport.app.state.session_factory

    async def report(ver: str, **extra) -> dict:
        r = await client.post("/api/agent/report", headers=agent, json=dict(REPORT, agent_version=ver, **extra))
        assert r.status_code == 200, r.text
        return r.json()

    async def target() -> str:
        async with factory() as s:
            return (await s.get(Server, sid)).target_agent_version

    await report("2.74")
    async with factory() as s:
        (await s.get(Server, sid)).target_agent_version = "2.75"
        await s.commit()
    assert (await report("2.74"))["update"] == {"version": "2.75"}
    # проба 2.75 отчитывается своей версией, но штатный бинарь еще 2.74: цель не снимаем
    await report("2.75", trial=True)
    assert await target() == "2.75"
    # проба упала до коммита - вернулась 2.74 и снова получает просьбу обновиться
    assert (await report("2.74", update_error="2.75: версия 2.75 не продержалась пробу"))["update"] == {"version": "2.75"}
    # проба принята: отчет без trial снимает цель, как раньше; старый агент поля не шлет
    await report("2.75", trial=True)
    await report("2.75")
    assert await target() == ""
