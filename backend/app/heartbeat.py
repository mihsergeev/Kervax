"""Хостовый «пульс» панели для внешнего сторожа (ops/panel-watchdog.sh).

Планировщик раз в ~минуту пишет файл heartbeat в DATA_DIR: метка времени, флаг
здоровья канала алертов и креды каналов. Хостовый watchdog (вне docker) читает
его и НЕЗАВИСИМО алертит, если пульс протух (панель/БД/шедулер мертвы) или канал
сломан — т.е. ловит ровно те случаи, когда сама панель сообщить о себе не может.
"""
import asyncio
import logging
import os
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import alerts, settings_store
from app.config import Settings

log = logging.getLogger("kervax.heartbeat")

# как часто писать пульс (независимо от длительности тика планировщика)
_HEARTBEAT_EVERY_S = 60.0
# сколько САМОпроверок канала подряд должны провалиться, прежде чем объявить его
# сломанным (alerts_ok=0). Единичный сбой — транзиент (блип сети/DNS сразу после
# рестарта при деплое, микро-недоступность api.telegram.org): гасим его, иначе
# хостовый watchdog ложно кричит «канал алертов сломан». Реальный отказ (протух
# токен, TG заблокирован) держится и валит все проверки → после N подряд алертим.
_CHANNEL_FAIL_STRIKES = 3


async def probe_alert_channel(cfg: dict) -> bool:
    """Активная само-проверка канала: Telegram getMe (валидность токена + связь).
    Ловит протухший токен и блокировку api.telegram.org. Вебхук активно не тестируем
    (не спамим) → если настроен только он, считаем канал рабочим. Один ретрай —
    чтобы разовый сетевой блип не выглядел как отказ."""
    if not alerts.alerts_enabled(cfg):
        return True  # каналов нет — проверять нечего
    token, chat = cfg.get("telegram_token"), cfg.get("telegram_chat")
    if not (token and chat):
        return True  # только вебхук — активно не проверяем
    base = (cfg.get("telegram_api") or "https://api.telegram.org").rstrip("/")
    for attempt in range(2):  # 1 ретрайт, чтобы сгладить одиночный сетевой блип
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(f"{base}/bot{token}/getMe")
                if r.status_code == 200 and r.json().get("ok") is True:
                    return True
                # 200-но-не-ok / 401 = невалидный токен → ретрай не поможет
                if r.status_code in (401, 404):
                    return False
        except Exception:  # noqa: BLE001 — таймаут/сеть: попробуем ещё раз
            pass
        if attempt == 0:
            await asyncio.sleep(2)
    return False


async def write_heartbeat(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings,
    cfg: dict, alerts_ok: bool,
) -> None:
    """Пишет свежий пульс в DATA_DIR/heartbeat (атомарно, права 600 — там креды).
    alerts_ok уже с дебаунсом (см. heartbeat_loop)."""
    try:
        lines = [
            f"ts={int(time.time())}",
            f"alerts_ok={1 if alerts_ok else 0}",
            f"tg_token={cfg.get('telegram_token', '')}",
            f"tg_chat={cfg.get('telegram_chat', '')}",
            f"tg_api={cfg.get('telegram_api', '')}",
            f"webhook={cfg.get('webhook', '')}",
            f"version={settings.version}",
            f"panel={settings.panel_url}",  # чтобы watchdog назвал КОНКРЕТНУЮ панель
        ]
        os.makedirs(settings.data_dir, exist_ok=True)
        path = os.path.join(settings.data_dir, "heartbeat")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)  # креды каналов — не мир-читаемо
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — пульс не должен ронять цикл планировщика
        log.warning("не удалось записать heartbeat", exc_info=True)


async def heartbeat_loop(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Пишет пульс на ФИКСИРОВАННОЙ каденции (60с), НЕЗАВИСИМО от длительности тика
    планировщика. Раньше пульс писался в конце тика — при большом числе мониторов
    тик может идти дольше, и пульс ложно «протухал» (watchdog кричал «зависла»,
    хотя панель просто занята). Независимая задача пишет пульс сразу на старте и
    далее раз в минуту; реальные отказы (процесс/цикл событий мёртв, БД недоступна)
    по-прежнему останавливают/валят запись → watchdog корректно сработает."""
    fail_streak = 0
    while True:
        try:
            async with session_factory() as session:
                cfg = await settings_store.get_alert_config(session, settings)
            fail_streak = 0 if await probe_alert_channel(cfg) else fail_streak + 1
            # канал считаем сломанным лишь после N ПОДРЯД неудач — гасим транзиенты
            # (деплой/сетевой блип), реальный отказ держится и после N подряд алертим
            alerts_ok = fail_streak < _CHANNEL_FAIL_STRIKES
            await write_heartbeat(session_factory, settings, cfg, alerts_ok)
        except Exception:  # noqa: BLE001 — пульс не должен ронять цикл планировщика
            log.warning("сбой цикла heartbeat", exc_info=True)
        await asyncio.sleep(_HEARTBEAT_EVERY_S)
