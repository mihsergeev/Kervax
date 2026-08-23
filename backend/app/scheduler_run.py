"""Отдельный процесс планировщика Kervax: сбор мониторов, серверные/локационные
алерты, прунинг, авто-бэкап. Запускать РОВНО в одном экземпляре — когда веб
масштабируется на несколько воркеров (те ставят KERVAX_RUN_SCHEDULER=0, иначе
планировщик запустился бы в каждом воркере).

Одиночный контейнер этот процесс НЕ использует: там планировщик крутится прямо
внутри веб-приложения (см. app.main lifespan).

  python -m app.scheduler_run
"""
import asyncio
import logging

from app.bootstrap import ensure_admin, ensure_default_location
from app.collector import collector_loop
from app.config import get_settings
from app.db import create_engine_and_factory

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx на INFO печатает URL каждого запроса, а токен Telegram-бота живёт ПРЯМО В URL
# (/bot<token>/sendMessage) — с ним в логах контейнера лежал бы валидный секрет,
# который читает любой, кто дотянулся до `docker logs`. Оставляем только warning+.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kervax.scheduler")


async def main() -> None:
    settings = get_settings()
    engine, factory = create_engine_and_factory(settings.db_url)
    # bootstrap идемпотентен (race-safe) — безопасно рядом с веб-воркерами
    await ensure_admin(factory, settings)
    await ensure_default_location(factory, settings)
    log.info("планировщик запущен (tick=%ss)", settings.scheduler_tick)
    try:
        await collector_loop(factory, settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
