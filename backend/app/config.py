from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KERVAX_", env_file=".env")

    app_name: str = "Kervax"
    version: str = "1.4.2"
    debug: bool = False
    # Внешний URL панели — если задан, в алерты добавляется ссылка на монитор
    # (напр. https://kervax.example.com). Пусто = ссылку не добавляем.
    panel_url: str = ""

    db_url: str = "sqlite+aiosqlite:///./data/kervax.db"
    data_dir: str = "./data"

    # --- Мониторы сайтов/сервисов (основной сценарий) ---
    # Тик планировщика: как часто фоновый цикл проверяет, каким мониторам пора
    # исполниться. Должен быть <= минимального interval среди мониторов.
    scheduler_tick: int = 30
    # Запускать ли фоновый планировщик (сбор/алерты/прунинг) внутри ЭТОГО процесса.
    # Одиночный контейнер: True (веб + планировщик вместе). Масштаб: веб-воркеры
    # ставят 0, а планировщик крутит отдельный одиночный процесс (app.scheduler_run).
    run_scheduler: bool = True
    default_check_interval: int = 60   # период проверки нового монитора, сек
    default_timeout_ms: int = 10000    # таймаут запроса, мс
    default_degraded_ms: int = 2000    # порог «медленно» → degraded, мс
    # Повторы при неуспехе: сколько раз перепроверить упавший монитор, прежде
    # чем считать его «плохим» (гасит одиночные блипы). И пауза между попытками.
    default_check_retries: int = 3
    check_retry_delay_ms: int = 1500
    # Разброс исходящих запросов, чтобы не бить по всем сайтам разом (меньше пик
    # трафика/нагрузки на панель). max_concurrency — потолок одновременных проверок
    # за тик (0 = без лимита); jitter — случайная задержка старта 0..N мс у каждой.
    # 50: заметно меньше пик, чем сотни разом, но тик успевает за интервалом даже
    # при массе медленных/падающих проверок (иначе тик растягивается на минуты).
    check_max_concurrency: int = 50
    check_jitter_ms: int = 400
    # Проверка через прокси-локацию медленнее на величину оверхеда прокси —
    # добавляем к порогу «медленно», чтобы прокси не помечались degraded зря.
    location_degraded_extra_ms: int = 2000
    # Смягчение проверки через прокси (даль + оверхед прокси): запас к таймауту и
    # внутренние повторы перед «недоступен» — далёкий, но живой сайт не мигает down.
    location_timeout_extra_ms: int = 10000
    location_retries: int = 2
    # Дефолтные пороги напоминаний (дней до истечения): эскалация SSL и домена.
    default_ssl_warn_days: list[int] = [14, 7, 1]
    default_domain_warn_days: list[int] = [7, 1]
    # Как часто обновлять «медленные» сроки (TLS/домен) — они меняются редко,
    # а RDAP-запросы лучше не частить. Часов.
    expiry_refresh_hours: int = 6
    # Сколько неудач подряд до алерта (гасит одиночные флапы)
    alert_after_failures: int = 3
    # Отдельный (больший) порог для деградации — «медленно» шумнее, чем полное падение
    degraded_after_failures: int = 5
    # Хранение тайм-серий результатов проверок, дней
    # 60 дней поминутной истории по каждому монитору почти никогда не нужны:
    # инциденты лежат отдельной таблицей и чисткой снимков не затрагиваются
    sample_retention_days: int = 30
    # Как часто прунить старые тайм-серии (сек). Граница ретеншена ползёт медленно,
    # незачем гонять DELETE каждый тик — раз в час достаточно.
    prune_interval_seconds: int = 3600
    # Как часто перепроверять сайты через прокси-локации (сек). Отдельная,
    # более редкая каденция — прокси медленнее и статус «из региона» меняется реже.
    location_probe_interval: int = 300

    # --- Серверы (агент-push) ---
    server_report_interval: int = 15   # как часто агент шлёт метрики, сек
    server_metric_retention_days: int = 30
    # Как часто класть метрики сервера в историю. Агент шлёт отчёты гораздо чаще
    # (это нужно для «онлайн» и порогов), но графику хватает точки в минуту —
    # а таблица метрик была самой большой в базе.
    server_metric_interval_seconds: int = 60
    # каталог с собранным агентом (бинари + install.sh), которые раздаёт панель
    agent_dist_dir: str = "/srv/agent"

    # Алерт «мало места на диске ноды»: порог в % (0 = выключить)
    disk_alert_percent: int = 90

    # --- Алерты (каналы могут переопределяться из UI) ---
    alert_telegram_token: str = ""
    alert_telegram_chat: str = ""
    # Базовый URL Telegram Bot API. Можно указать свой прокси/зеркало, если
    # api.telegram.org заблокирован в регионе (напр. https://api-tg.mydomain.com).
    alert_telegram_api: str = "https://api.telegram.org"
    alert_webhook: str = ""
    # антифлуд: при ≥ N новых алертов за один цикл шлём один дайджест вместо потока
    # (0 = выключить группировку)
    alert_flood_threshold: int = 6

    # --- Авто-бэкап БД ---
    backup_interval_hours: int = 24
    backup_keep: int = 14

    # --- Аутентификация ---
    jwt_secret: str = "dev-insecure-change-me"
    jwt_ttl_minutes: int = 12 * 60

    # Начальная учётка админа (сидируется при первом старте; далее пароль
    # меняется в UI и из .env НЕ пересинхронизируется)
    admin_user: str = "admin"
    admin_password: str = "admin"

    # Аварийный сброс (break-glass): при KERVAX_ADMIN_PASSWORD_RESET=1 старт
    # сбрасывает пароль админа на admin_password и отключает 2FA. Убрать флаг
    # из .env после входа. Нужно, если потерян пароль И 2FA.
    admin_password_reset: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
