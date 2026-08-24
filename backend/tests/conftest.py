from collections.abc import AsyncIterator

import httpx
import pytest

from app import config


@pytest.fixture
async def client(tmp_path, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    db_path = (tmp_path / "test.db").as_posix()
    monkeypatch.setenv("KERVAX_DB_URL", f"sqlite+aiosqlite:///{db_path}")
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


# Фикстуры сброса лимитера больше нет: счётчик неудачных входов лежит в базе,
# а база у каждого теста своя временная — сбрасывать глобальное состояние нечего.


@pytest.fixture
async def auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "testpass-2026"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
