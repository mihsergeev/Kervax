from app import checks as checks_mod
from app.checks import ExpiryInfo, _parse_date, _registrable_domain, _to_ascii
from app.collector import _apply_expiry
from app.models import Check


def test_registrable_domain():
    assert _registrable_domain("https://www.example.com/path?x=1") == "example.com"
    assert _registrable_domain("example.com") == "example.com"
    assert _registrable_domain("http://a.b.example.co.uk") == "example.co.uk"
    assert _registrable_domain("shop.example.com.cy:443") == "example.com.cy"
    assert _registrable_domain("localhost") == "localhost"


def test_parse_date():
    assert _parse_date("2026-08-01T21:00:00Z").year == 2026
    assert _parse_date("2026.08.01").month == 8
    assert _parse_date("мусор") is None


def test_to_ascii_idn():
    assert _to_ascii("example.com") == "example.com"
    assert _to_ascii("пример.рф") == "xn--e1afmkfd.xn--p1ai"


async def test_domain_days_routes_ru_to_whois(monkeypatch):
    called = {}

    async def fake_whois(domain, timeout):
        called["whois"] = domain
        return 42, "ok"

    async def fake_rdap(domain, timeout):
        called["rdap"] = domain
        return 100, "ok"

    monkeypatch.setattr("app.checks._domain_days_whois_ru", fake_whois)
    monkeypatch.setattr("app.checks._domain_days_rdap", fake_rdap)

    assert await checks_mod._domain_days("yandex.ru", 10.0) == (42, "ok")
    assert await checks_mod._domain_days("пример.рф", 10.0) == (42, "ok")
    assert await checks_mod._domain_days("example.com", 10.0) == (100, "ok")
    assert called == {"whois": "пример.рф", "rdap": "example.com"}


def _row(**kw):
    base = dict(
        id=1, name="site", type="http", target="https://x",
        check_ssl=True, ssl_warn_days=[14, 7, 1], ssl_alerted_days=None,
        check_domain=True, domain_warn_days=[7, 1], domain_alerted_days=None,
    )
    base.update(kw)
    return Check(**base)


def _apply(row, info):
    """Прогоняет _apply_expiry и эмулирует успешную отправку (ставит alerted_days)."""
    pending: list = []
    _apply_expiry(row, info, None, pending)
    for p in pending:
        flag = p[6]
        if flag:
            setattr(row, flag[0], flag[1])
    return pending


def test_apply_expiry_ssl_escalation():
    row = _row()
    # 10 дн (порог 14) → первый алерт, флаг=14
    p = _apply(row, ExpiryInfo(ssl_days=10, ssl_message="ok"))
    assert len(p) == 1 and p[0][0] == "ssl" and row.ssl_alerted_days == 14
    # 9 дн — тот же порог → без нового алерта
    assert _apply(row, ExpiryInfo(ssl_days=9, ssl_message="ok")) == []
    # 6 дн (порог 7) → следующий алерт
    assert len(_apply(row, ExpiryInfo(ssl_days=6, ssl_message="ok"))) == 1
    assert row.ssl_alerted_days == 7
    # 0 дн (порог 1) → последний алерт
    assert len(_apply(row, ExpiryInfo(ssl_days=0, ssl_message="ok"))) == 1
    assert row.ssl_alerted_days == 1
    # Перевыпустили (170 дн) → эскалация сброшена И пришло подтверждение.
    # Раньше здесь была тишина: человек продлевал по алерту панели и не знал,
    # увидела ли она это.
    done = _apply(row, ExpiryInfo(ssl_days=170, ssl_message="ok"))
    assert len(done) == 1 and done[0][0] == "ssl"
    assert "перевыпущен" in done[0][3] and done[0][7] == "✅"
    assert row.ssl_alerted_days is None
    # повторный проход по уже сброшенной эскалации молчит
    assert _apply(row, ExpiryInfo(ssl_days=170, ssl_message="ok")) == []


def test_apply_expiry_domain_and_invalid_ssl_no_alert():
    row = _row()
    # домен 6 дн (порог 7) → алерт про домен
    p = _apply(row, ExpiryInfo(domain_days=6, domain_message="ok"))
    assert len(p) == 1 and p[0][0] == "domain" and row.domain_alerted_days == 7
    # 20 дн (вне порогов) → подтверждение продления (эскалация была)
    renew = _apply(row, ExpiryInfo(domain_days=20, domain_message="ok"))
    assert len(renew) == 1 and "продлена" in renew[0][3]
    assert row.domain_alerted_days is None
    # дальше молчим: подтверждение шлётся один раз, на переходе
    assert _apply(row, ExpiryInfo(domain_days=20, domain_message="ok")) == []
    # невалидный TLS (ssl_days None) НЕ порождает алерт здесь
    assert _apply(row, ExpiryInfo(ssl_days=None, ssl_message="невалиден")) == []
