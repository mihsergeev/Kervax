"""Хостовый «пульс» панели для внешнего сторожа (ops/panel-watchdog.sh).

Планировщик раз в ~минуту пишет файл heartbeat в DATA_DIR: метка времени, флаг
здоровья канала алертов и креды каналов. Хостовый watchdog (вне docker) читает
его и НЕЗАВИСИМО алертит, если пульс протух (панель/БД/шедулер мертвы) или канал
сломан — т.е. ловит ровно те случаи, когда сама панель сообщить о себе не может.
"""
import asyncio
import contextvars
import logging
import os
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import alerts, panel_errors, settings_store
from app.config import Settings

log = logging.getLogger("kervax.heartbeat")

# Код идет из цикла планировщика (и его фоновых задач: они наследуют контекст). В одиночном
# режиме планировщик крутится в веб-процессе, а ошибки HTTP-запросов журнал получает от
# middleware - их второй раз не пишем.
IN_SCHEDULER: contextvars.ContextVar[bool] = contextvars.ContextVar("kervax_in_scheduler", default=False)


class SchedulerErrors(logging.Handler):
    """Ошибки планировщика (log.exception и log.error логгеров kervax.*) - в журнал ошибок панели.
    Стадия, упавшая на NameError, раньше оставляла строку только в docker-логе scheduler, а он
    пропадает с каждой выкладкой. Одинаковые складываются по тексту и типу исключения."""

    def __init__(self, session_factory) -> None:
        super().__init__(level=logging.ERROR)
        self.session_factory = session_factory

    def emit(self, record: logging.LogRecord) -> None:
        if not IN_SCHEDULER.get() or record.name == panel_errors.log.name:
            return
        try:
            msg = record.getMessage()
            exc = record.exc_info[1] if record.exc_info else None
            detail = panel_errors.short_tb(exc) if exc is not None else ""
            what = f"{type(exc).__name__}: {exc}" if exc is not None else ""
            panel_errors.record_soon(
                self.session_factory, kind="server", action="планировщик",
                message=msg + (f" - {what}" if what else ""), detail=detail,
                fp_key=f"{record.name}|{msg}|{type(exc).__name__ if exc is not None else ''}")
        except Exception:  # noqa: BLE001 - журнал не должен ронять планировщик
            self.handleError(record)

# как часто писать пульс (независимо от длительности тика планировщика)
_HEARTBEAT_EVERY_S = 60.0
# сколько САМОпроверок канала подряд должны провалиться, прежде чем объявить его
# сломанным (alerts_ok=0). Единичный сбой — транзиент (блип сети/DNS сразу после
# рестарта при деплое, микро-недоступность api.telegram.org): гасим его, иначе
# хостовый watchdog ложно кричит «канал алертов сломан». Реальный отказ (протух
# токен, TG заблокирован) держится и валит все проверки → после N подряд алертим.
_CHANNEL_FAIL_STRIKES = 3
# Пульс пишет своя задача, и повисший цикл планировщика (await без таймаута, ждет блокировку в
# базе) он не замечал: процесс жив, пульс свежий, а мониторы не проверяются. Поэтому в пульсе
# еще время начала последнего тика (tick) и порог, после которого тик считается повисшим
# (tick_max, щедрый: длинный тик не должен шуметь), и стадия, которая падает тиков подряд
# (stage_fail). Сторож (ops/panel-watchdog.sh) тревожит по ним сам.
_TICK_MAX_MIN = 600
_TICK_MAX_TICKS = 10
_tick_at = 0.0
_stage_fails: dict[str, tuple[int, str]] = {}  # стадия -> (сколько тиков подряд, что упало)
_partial: dict[str, str] = {}


def tick_started(now: float | None = None) -> None:
    """Начался тик планировщика."""
    global _tick_at
    _tick_at = time.time() if now is None else now


def stage_partial(name: str, what: str) -> None:
    """Стадия отработала, но не целиком (серверные алерты пропустили сервер с ошибкой)."""
    _partial[name] = what


def stage_done(name: str, ok: bool, what: str = "") -> None:
    """Итог стадии за тик: упала или отработала не целиком - счетчик подряд растет."""
    part = _partial.pop(name, "")
    if ok and not part:
        _stage_fails.pop(name, None)
        return
    n = _stage_fails.get(name, (0, ""))[0] + 1
    _stage_fails[name] = (n, what or part)


def stage_fail_line() -> str:
    """Худшая стадия для пульса: "5 серверные алерты (node-a)"; пусто - все стадии в порядке."""
    if not _stage_fails:
        return ""
    name, (n, what) = max(_stage_fails.items(), key=lambda x: x[1][0])
    return f"{n} {name}" + (f" ({what})" if what else "")


def reset() -> None:
    """Для тестов."""
    global _tick_at
    _tick_at = 0.0
    _stage_fails.clear()
    _partial.clear()


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
            # начало последнего тика (0 - планировщик выключен или еще не начал) и когда он повис
            f"tick={int(_tick_at)}",
            f"tick_max={max(_TICK_MAX_MIN, _TICK_MAX_TICKS * int(settings.scheduler_tick or 0))}",
            # одной строкой: перевод строки в имени сломал бы разбор файла сторожем
            f"stage_fail={' '.join(stage_fail_line().split())}",
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
