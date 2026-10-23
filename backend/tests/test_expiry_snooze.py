"""Отложенные сроки сайта (app/expiry_snooze.py): "напомнить за 7 дней", "за 1 день", "не следить".

Повод: до конца регистрации домена 6 дней, продлят его в последний день, а пункт на главной и
напоминание все это время висят. Отложить - пункт уходит, пока до конца больше выбранного; на
пороге отложки возвращается и алерт приходит. Продлили - отложка снимается сама, "не следить"
остается."""

from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import expiry_snooze
from app.checks import ExpiryInfo
from app.collector import DomainAlerts, _apply_expiry
from app.models import AuditLog, Check

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


def _row(**kw):
    base = dict(
        id=1, name="site", type="http", target="https://site-a.example.com",
        check_ssl=True, ssl_warn_days=[7, 1], ssl_alerted_days=None,
        check_domain=True, domain_warn_days=[7, 1], domain_alerted_days=None,
    )
    base.update(kw)
    return Check(**base)


def _apply(row, **info):
    pending: list = []
    _apply_expiry(row, ExpiryInfo(**info), NOW, pending)
    for p in pending:
        if p[6]:
            setattr(row, p[6][0], p[6][1])
    return pending


def _snooze(row, kind, mode, days):
    row.expiry_snooze = {**(row.expiry_snooze or {}), kind: expiry_snooze.entry(mode, days, "admin", NOW)}


def test_remind_one_day_before_hides_until_last_day_then_alerts():
    row = _row()
    # 6 дней: порог 7 - напоминание пришло
    assert len(_apply(row, domain_days=6)) == 1 and row.domain_alerted_days == 7
    _snooze(row, "domain", 1, 6)
    assert expiry_snooze.hidden(row, "domain")
    # дни идут, пункт скрыт, молчим
    for d in (5, 4, 3, 2):
        assert _apply(row, domain_days=d) == [] and expiry_snooze.hidden(row, "domain")
    # последний день: пункт вернулся и алерт на пороге 1
    p = _apply(row, domain_days=1)
    assert len(p) == 1 and "через 1 дн." in p[0][3] and row.domain_alerted_days == 1
    assert not expiry_snooze.hidden(row, "domain")
    # отложка на месте до продления, но больше не прячет и не мешает следующему алерту
    assert len(_apply(row, domain_days=-1)) == 1


def test_snooze_set_before_first_alert_does_not_eat_the_threshold():
    """Отложили, когда напоминание еще не уходило: порог не отмечается молча, алерт придет на
    пороге отложки."""
    row = _row(domain_warn_days=[30, 7, 1])
    _snooze(row, "domain", 7, 25)
    assert _apply(row, domain_days=25) == [] and row.domain_alerted_days is None
    p = _apply(row, domain_days=7)
    assert len(p) == 1 and row.domain_alerted_days == 7


def test_remind_seven_days_alerts_on_seven_even_without_such_threshold():
    """"Напомнить за 7 дней" - алерт на 7, даже если у монитора пороги [30, 1]."""
    row = _row(domain_warn_days=[30, 1])
    assert len(_apply(row, domain_days=20)) == 1 and row.domain_alerted_days == 30
    _snooze(row, "domain", 7, 20)
    assert _apply(row, domain_days=8) == []
    p = _apply(row, domain_days=7)
    assert len(p) == 1 and row.domain_alerted_days == 7
    assert len(_apply(row, domain_days=1)) == 1


def test_renewal_lifts_snooze_but_not_off():
    row = _row()
    _apply(row, domain_days=6)
    _snooze(row, "domain", 1, 6)
    # продлили на год: отложка снята, пришло "продлена"
    p = _apply(row, domain_days=371)
    assert len(p) == 1 and "продлена" in p[0][3]
    assert row.expiry_snooze is None and row.domain_alerted_days is None
    # через год снова по обычным порогам
    assert len(_apply(row, domain_days=7)) == 1

    # "не следить" продление не снимает и молчит всегда
    row = _row()
    _snooze(row, "domain", 0, 6)
    assert _apply(row, domain_days=6) == []
    assert _apply(row, domain_days=371) == [] and expiry_snooze.get(row, "domain")["mode"] == 0
    assert _apply(row, domain_days=0) == [] and expiry_snooze.hidden(row, "domain")


def test_day_jitter_is_not_renewal():
    """RDAP и WHOIS считают дату по-разному, срок плавает на день: это не продление."""
    row = _row()
    _snooze(row, "domain", 1, 6)
    _apply(row, domain_days=7)
    assert expiry_snooze.get(row, "domain") is not None


def test_ssl_snooze_same_rules():
    row = _row()
    assert len(_apply(row, ssl_days=5)) == 1
    _snooze(row, "ssl", 1, 5)
    assert _apply(row, ssl_days=3) == []
    assert len(_apply(row, ssl_days=1)) == 1
    # перевыпустили: отложка снята, "перевыпущен" пришло
    p = _apply(row, ssl_days=89)
    assert "перевыпущен" in p[0][3] and row.expiry_snooze is None


def test_domain_snooze_silences_all_monitors_of_the_domain():
    rows = [_row(id=i, target=f"https://s{i}.example.com") for i in (1, 2)]
    for r in rows:
        _snooze(r, "domain", 1, 6)
    doms = DomainAlerts([(r.id, r.target, r.name, None, 6) for r in rows])
    sent = []
    for r in rows:
        pending: list = []
        _apply_expiry(r, ExpiryInfo(domain_days=6), NOW, pending, doms)
        sent += pending
    assert sent == []


async def test_api_snooze_whole_domain_and_audit(client: httpx.AsyncClient, auth_headers):
    ids = []
    for host in ("a", "b"):
        r = await client.post("/api/checks", headers=auth_headers,
                              json={"name": f"{host}-site", "type": "http", "target": f"https://{host}.example.com"})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    r = await client.post("/api/checks", headers=auth_headers,
                          json={"name": "other", "type": "http", "target": "https://example.org"})
    other = r.json()["id"]
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as s:
        for c in await s.scalars(select(Check)):
            c.domain_days, c.ssl_days = 6, 40
        await s.commit()

    url = f"/api/checks/{ids[0]}/expiry-snooze"
    # до конца 6 дней: "за 7" уже не к месту
    r = await client.post(url, headers=auth_headers, json={"kind": "domain", "mode": 7})
    assert r.status_code == 400
    r = await client.post(url, headers=auth_headers, json={"kind": "domain", "mode": 1})
    assert r.status_code == 200, r.text
    got = r.json()["expiry_snooze"]["domain"]
    assert got["mode"] == 1 and got["by"] == "admin" and got["days"] == 6 and got["at"]
    async with factory() as s:
        snz = {c.id: c.expiry_snooze for c in await s.scalars(select(Check))}
        acts = [(a.action, a.target, a.detail) for a in await s.scalars(select(AuditLog))]
    # отметка домена - всем мониторам на нем, чужой домен не тронут
    assert snz[ids[1]]["domain"]["mode"] == 1 and snz[other] is None
    assert ("expiry_snooze", "example.com", "домен: напомнить за 1 дн.") in acts

    # сертификат - только этому монитору
    r = await client.post(url, headers=auth_headers, json={"kind": "ssl", "mode": 7})
    assert r.json()["expiry_snooze"]["ssl"]["mode"] == 7
    r = await client.get(f"/api/checks/{ids[1]}", headers=auth_headers)
    assert "ssl" not in r.json()["expiry_snooze"]

    # вернуть
    r = await client.post(url, headers=auth_headers, json={"kind": "domain", "mode": None})
    assert "domain" not in r.json()["expiry_snooze"]
    r = await client.post(url, headers=auth_headers, json={"kind": "ssl", "mode": None})
    assert r.json()["expiry_snooze"] is None
    async with factory() as s:
        assert (await s.get(Check, ids[1])).expiry_snooze is None
        acts = [a.action for a in await s.scalars(select(AuditLog))]
    assert acts.count("expiry_watch") == 2
    # "не следить" доступно всегда
    r = await client.post(url, headers=auth_headers, json={"kind": "domain", "mode": 0})
    assert r.json()["expiry_snooze"]["domain"]["mode"] == 0
