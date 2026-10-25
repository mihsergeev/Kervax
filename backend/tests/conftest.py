import os
from collections.abc import AsyncIterator

import httpx
import pytest

from app import config


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    # KERVAX_TEST_PG=postgresql+asyncpg://... - те же тесты на PostgreSQL (CI, job migrations):
    # SQLite не ловит aware datetime, JSON-операции и сдвиг sequence после вставки с явным id.
    # База одна на прогон, поэтому без -n, и перед каждым тестом схема создается заново
    pg = os.environ.get("KERVAX_TEST_PG", "")
    db_path = (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("KERVAX_DB_URL", pg or f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_ADMIN_USER", "admin")
    monkeypatch.setenv("KERVAX_ADMIN_PASSWORD", "testpass-2026")
    monkeypatch.setenv("KERVAX_JWT_SECRET", "test-secret-0123456789abcdef0123456789abcdef")
    config.get_settings.cache_clear()

    from app.bootstrap import ensure_admin
    from app.db import Base
    from app.main import create_app

    app = create_app()
    async with app.state.engine.begin() as conn:
        if pg:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await ensure_admin(app.state.session_factory, config.get_settings())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await app.state.engine.dispose()
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _panel_started_long_ago():
    """Сдвигает «панель только что поднялась» в прошлое.

    collector фиксирует _PANEL_STARTED в момент ИМПОРТА, а первые минуты после
    старта оффлайн-алерты сознательно не шлются (тогда молчат все ноды, и это
    пауза панели, а не их). В тестах импорт всегда «только что», поэтому любой
    тест про оффлайн молча проверял пустоту — пять таких висели красными.
    """
    from datetime import timedelta

    from app import collector

    original = collector._PANEL_STARTED
    collector._PANEL_STARTED = original - timedelta(hours=1)
    yield
    collector._PANEL_STARTED = original


@pytest.fixture(autouse=True)
def _servers_added_long_ago(monkeypatch):
    """Без тишины для только что добавленного сервера: в тестах сервер всегда создан "сейчас",
    и любой тест на алерт проверял бы пустоту. Саму тишину проверяет свой тест."""
    from app import collector

    monkeypatch.setattr(collector, "_NEW_SERVER_QUIET", 0)


def pytest_configure(config):
    config.addinivalue_line("markers", "calm: тест с выдержкой отбоя и повторами после отбоя (collector._SETTLE_S)")


@pytest.fixture(autouse=True)
def _calm_off(request, monkeypatch):
    """Без общей выдержки отбоя и тихих повторов (collector._SETTLE_S, _AGAIN_S): тесты каждого вида
    проверяют его собственный отбой тиком позже. Само правило проверяют тесты с меткой calm."""
    if request.node.get_closest_marker("calm"):
        return
    from app import collector

    monkeypatch.setattr(collector, "_SETTLE_S", 0)
    monkeypatch.setattr(collector, "_AGAIN_S", 0)


@pytest.fixture(autouse=True)
def _no_dns_lookups(monkeypatch):
    """Мониторы из панели меряют DNS отдельным запросом (checks._dns_time). В тестах сеть не
    трогаем: домены там выдуманные, а резолвер на машине разработчика бывает медленным."""
    from app import checks

    async def no_lookup(url, timeout):
        return None, True

    monkeypatch.setattr(checks, "_dns_time", no_lookup)

    # адреса доменов для поиска сервера монитора (app/parents.py): по умолчанию домены не
    # резолвятся, тест про это подставляет свои
    from app import parents

    async def no_addrs(host):
        return frozenset()

    monkeypatch.setattr(parents, "_lookup", no_addrs)
    monkeypatch.setattr(parents, "_dns", {})

    # обратное имя адреса для подписи группы мониторов (app/site_group.py)
    from app import site_group

    async def no_ptr(ip):
        raise OSError("в тестах без DNS")

    monkeypatch.setattr(site_group, "_ptr", no_ptr)
    monkeypatch.setattr(site_group, "_ptr_cache", {})

    # куда панели можно ходить (app/net_guard.py): имена не резолвим, судим по самому адресу
    from app import net_guard

    async def no_resolve(host, timeout):
        return []

    monkeypatch.setattr(net_guard, "_resolve", no_resolve)


@pytest.fixture(autouse=True)
def _scheduler_memory_clean():
    """Память планировщика между тиками (отбои мониторов на повтор, отметка массового молчания
    парка): тесту - с чистого листа, иначе отметка одного теста дает отбой в следующем."""
    from app import collector

    collector._REC_RETRY.clear()
    collector._gap_said.clear()
    yield
    collector._REC_RETRY.clear()
    collector._gap_said.clear()


@pytest.fixture(autouse=True)
def _job_logs_clean():
    """Запросы логов упавших Job живут в памяти планировщика: тесту - с чистого листа."""
    from app import job_logs

    job_logs.reset()
    yield
    job_logs.reset()


# Фикстуры сброса лимитера больше нет: счётчик неудачных входов лежит в базе,
# а база у каждого теста своя временная — сбрасывать глобальное состояние нечего.


@pytest.fixture
async def auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "testpass-2026"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
