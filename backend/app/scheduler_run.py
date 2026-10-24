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
import signal

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
    # docker stop шлет SIGTERM: доделываем тик и выходим (collector_loop смотрит stop после
    # тика). По умолчанию Python умирал сразу, и убитый между отправкой алерта и записью
    # состояния планировщик после старта слал тот же алерт второй раз
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: сигналов в цикле событий нет
            pass
    try:
        await collector_loop(factory, settings, stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
