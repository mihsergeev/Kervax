import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security import MIN_PASSWORD_LEN


def _clean_warn_days(v: list[int] | None) -> list[int] | None:
    """Нормализует список порогов: только 1..3650, без дублей, по убыванию, ≤6."""
    if v is None:
        return None
    out = sorted({int(x) for x in v if 1 <= int(x) <= 3650}, reverse=True)
    return out[:6]

CheckType = Literal["http", "tcp_port", "cert"]


class LoginRequest(BaseModel):
    username: str
    password: str
    otp: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str = "admin"
    # пусто = все разделы / все группы (см. модель User). Группы серверов и сайтов
    # раздельные: наборы имён у них свои
    sections: list[str] | None = None
    server_groups: list[str] | None = None
    site_groups: list[str] | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=128)
    # роль новой учётки; по умолчанию — только просмотр
    role: str = Field(default="viewer", pattern="^(admin|editor|viewer)$")
    # пусто = без ограничений (все разделы / все группы)
    sections: list[str] = Field(default_factory=list, max_length=20)
    server_groups: list[str] = Field(default_factory=list, max_length=100)
    site_groups: list[str] = Field(default_factory=list, max_length=100)


class UserUpdate(BaseModel):
    """Правка учётки админом: роль и границы доступа. Пароль — отдельным маршрутом."""

    role: str | None = Field(default=None, pattern="^(admin|editor|viewer)$")
    sections: list[str] | None = Field(default=None, max_length=20)
    server_groups: list[str] | None = Field(default=None, max_length=100)
    site_groups: list[str] | None = Field(default=None, max_length=100)


class UserResetPassword(BaseModel):
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=128)


class UserListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    # пусто = все разделы / все группы
    sections: list[str] | None = None
    server_groups: list[str] | None = None
    site_groups: list[str] | None = None
    totp_enabled: bool
    created_at: datetime


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=128)


class TwoFAStatusOut(BaseModel):
    enabled: bool


class TwoFASetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class TwoFAVerifyRequest(BaseModel):
    otp: str = Field(min_length=1, max_length=16)


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ts: datetime
    username: str
    action: str
    target: str
    detail: str


# --- мониторы сайтов/сервисов ---

_METHOD = "^(GET|HEAD|POST|PUT|OPTIONS)$"


class CheckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: CheckType
    target: str = Field(min_length=1, max_length=512)
    port: int = Field(default=0, ge=0, le=65535)
    enabled: bool = True
    group_name: str = Field(default="", max_length=64)
    interval_seconds: int = Field(default=60, ge=10, le=86400)
    timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    degraded_ms: int = Field(default=2000, ge=1, le=60000)
    retries: int = Field(default=2, ge=0, le=10)
    alert_after_failures: int = Field(default=2, ge=1, le=20)
    degraded_after_failures: int = Field(default=5, ge=1, le=100)
    method: str = Field(default="GET", pattern=_METHOD)
    expected_status: str = Field(default="200-399", max_length=32)
    keyword_up: str = Field(default="", max_length=256)
    keyword_down: str = Field(default="", max_length=256)
    # HTTP-аутентификация: "" = нет, "basic" = HTTP Basic (логин/пароль)
    auth_method: str = Field(default="", pattern="^(|basic)$")
    auth_user: str = Field(default="", max_length=128)
    auth_pass: str = Field(default="", max_length=256)
    http_headers: str = Field(default="", max_length=4096)
    ignore_tls: bool = False
    check_all_ips: bool = False
    check_ssl: bool = True
    check_domain: bool = True
    ssl_warn_days: list[int] = [14, 7, 1]
    domain_warn_days: list[int] = [7, 1]
    check_locations: bool = False  # локации опциональны — по умолчанию не через прокси
    location_ids: list[int] | None = None
    alert_mutes: list[str] | None = None  # типы алертов, заглушённые для этого монитора

    _clean = field_validator("ssl_warn_days", "domain_warn_days")(_clean_warn_days)


class CheckUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    type: CheckType | None = None
    target: str | None = Field(default=None, min_length=1, max_length=512)
    port: int | None = Field(default=None, ge=0, le=65535)
    enabled: bool | None = None
    group_name: str | None = Field(default=None, max_length=64)
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    timeout_ms: int | None = Field(default=None, ge=1000, le=60000)
    degraded_ms: int | None = Field(default=None, ge=1, le=60000)
    retries: int | None = Field(default=None, ge=0, le=10)
    alert_after_failures: int | None = Field(default=None, ge=1, le=20)
    degraded_after_failures: int | None = Field(default=None, ge=1, le=100)
    method: str | None = Field(default=None, pattern=_METHOD)
    expected_status: str | None = Field(default=None, max_length=32)
    keyword_up: str | None = Field(default=None, max_length=256)
    keyword_down: str | None = Field(default=None, max_length=256)
    auth_method: str | None = Field(default=None, pattern="^(|basic)$")
    auth_user: str | None = Field(default=None, max_length=128)
    auth_pass: str | None = Field(default=None, max_length=256)
    http_headers: str | None = Field(default=None, max_length=4096)
    ignore_tls: bool | None = None
    check_all_ips: bool | None = None
    check_ssl: bool | None = None
    check_domain: bool | None = None
    ssl_warn_days: list[int] | None = None
    domain_warn_days: list[int] | None = None
    check_locations: bool | None = None
    location_ids: list[int] | None = None
    alert_mutes: list[str] | None = None

    _clean = field_validator("ssl_warn_days", "domain_warn_days")(_clean_warn_days)


class CheckBulkUpdate(BaseModel):
    """Массовое применение настроек к мониторам (только переданные поля).
    ids=None → ко ВСЕМ; ids=[…] → только к выбранным."""

    ids: list[int] | None = Field(default=None, max_length=5000)
    interval_seconds: int | None = Field(default=None, ge=10, le=86400)
    timeout_ms: int | None = Field(default=None, ge=1000, le=60000)
    degraded_ms: int | None = Field(default=None, ge=1, le=60000)
    retries: int | None = Field(default=None, ge=0, le=10)
    alert_after_failures: int | None = Field(default=None, ge=1, le=20)
    degraded_after_failures: int | None = Field(default=None, ge=1, le=100)
    method: str | None = Field(default=None, pattern=_METHOD)
    expected_status: str | None = Field(default=None, max_length=32)
    check_ssl: bool | None = None
    check_domain: bool | None = None
    check_locations: bool | None = None
    check_all_ips: bool | None = None
    ssl_warn_days: list[int] | None = None
    domain_warn_days: list[int] | None = None
    location_ids: list[int] | None = None

    _clean = field_validator("ssl_warn_days", "domain_warn_days")(_clean_warn_days)


class BulkResult(BaseModel):
    updated: int


class CheckOrderItem(BaseModel):
    id: int
    # если задано — переназначить группу монитора (перетаскивание между группами)
    group_name: str | None = Field(default=None, max_length=64)


class SnoozeIn(BaseModel):
    """Быстрое приглушение алертов: заглушить на N часов (0 = снять снуз)."""

    hours: float = Field(ge=0, le=24 * 30)  # до 30 дней


class AlertSnoozeIn(BaseModel):
    """Точечный снуз ОДНОГО типа алерта сервера на N часов (0 = снять)."""

    # «disk» или «disk@2» — тип, опционально с макс. заглушаемым уровнем (см. _muted)
    kind: str = Field(min_length=1, max_length=32, pattern=r"^[a-z_]+(@[1-3])?$")
    hours: float = Field(ge=0, le=24 * 30)


class CheckIdList(BaseModel):
    """Список id мониторов для массовой операции (удаление и т.п.)."""

    ids: list[int] = Field(min_length=1, max_length=2000)


class CheckReorder(BaseModel):
    """Полный список мониторов в новом порядке отображения; group_name (если
    задан) переназначает группу — так одним запросом сохраняются и порядок
    мониторов, и порядок групп, и перенос монитора в другую группу."""

    order: list[CheckOrderItem] = Field(min_length=1, max_length=2000)


class CheckImport(BaseModel):
    """Массовое создание мониторов из списка (каждый — валидируется как CheckCreate)."""

    items: list[CheckCreate] = Field(min_length=1, max_length=500)


class TelegramOut(BaseModel):
    """Состояние персональных алертов учётки (токен наружу не отдаём)."""

    chat_id: str
    own_token: bool  # шлём своим ботом, а не общим ботом панели
    alerts: bool
    bot: str  # @имя бота, которому надо писать (пусто = бот не настроен)
    ready: bool  # есть и чат, и хоть какой-то токен — доставка возможна


class TelegramLinkOut(BaseModel):
    code: str  # что отправить боту
    bot: str


class TelegramUpdate(BaseModel):
    alerts: bool | None = None
    chat_id: str | None = Field(default=None, max_length=64)
    token: str | None = Field(default=None, max_length=256)


class BrandingOut(BaseModel):
    """Состояние брендирования. Отдаётся публично — рисуется на экране входа."""

    logo: bool          # загружен ли свой логотип
    title: str          # подпись рядом с логотипом (название компании)
    plate: str          # auto | always | never — подложка под логотип
    # что решил автоанализ картинки при загрузке (края непрозрачны или логотип
    # тёмный → на тёмной теме нужна светлая подложка). Считает браузер: только он
    # видит пиксели, а тащить на бэкенд декодер картинок ради этого не стоит.
    plate_auto: bool = False
    version: int        # растёт при замене; используется как ?v= для кэша


class BrandingIn(BaseModel):
    data: str = Field(min_length=1, max_length=1_400_000)  # base64/data-URL
    plate: str = Field(default="auto", pattern="^(auto|always|never)$")
    plate_auto: bool = False  # вердикт автоанализа на стороне браузера
    title: str = Field(default="", max_length=64)


class KnownHostsOut(BaseModel):
    """Что уже под мониторингом — для галочек рядом с доменами в «Сервисах»."""

    # домен → id монитора; 0 = мониторится, но в группе, невидимой этой учётке
    # (id не отдаём, а дубль всё равно не дадим создать)
    hosts: dict[str, int]
    groups: list[str]  # существующие группы сайтов — выбор при массовом добавлении
    ignored: list[str]  # помеченные «мониторить не нужно»


class DiscoveredDomain(BaseModel):
    domain: str
    servers: list[str]  # где встретился (имена нод) — подсказка «чей это домен»


class DiscoveredOut(BaseModel):
    """Всё, что агенты нашли на веб-серверах парка, + что из этого уже мониторится."""

    domains: list[DiscoveredDomain]
    hosts: dict[str, int]
    groups: list[str]
    ignored: list[str]  # помеченные «не нужен» — из счётчика и плашки исключены


class IgnoreDomainsIn(BaseModel):
    """Пометить домены ненужными (ignore=true) или вернуть в предложения."""

    domains: list[str] = Field(min_length=1, max_length=2000)
    ignore: bool = True


class AdoptDomainsIn(BaseModel):
    """Завести мониторы по доменам, которые агент нашёл на веб-сервере."""

    domains: list[str] = Field(min_length=1, max_length=500)
    group_name: str = Field(default="", max_length=64)


class AdoptResult(BaseModel):
    created: int
    # «домен — причина», по-человечески: wildcard и мусор из server_name не мониторятся
    skipped: list[str]
    hosts: dict[str, int]  # обновлённая карта, чтобы UI перерисовал галочки без перезагрузки


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    target: str
    port: int
    enabled: bool
    group_name: str
    sort_order: int
    interval_seconds: int
    timeout_ms: int
    degraded_ms: int
    retries: int
    alert_after_failures: int
    degraded_after_failures: int
    method: str
    expected_status: str
    keyword_up: str
    keyword_down: str
    auth_method: str = ""
    auth_user: str = ""
    auth_pass: str = ""
    http_headers: str = ""
    ignore_tls: bool = False
    check_all_ips: bool = False
    check_ssl: bool
    check_domain: bool
    ssl_warn_days: list[int]
    domain_warn_days: list[int]
    ssl_days: int | None
    domain_days: int | None
    ssl_message: str
    domain_message: str
    expiry_checked_at: datetime | None
    check_locations: bool
    location_ids: list[int] | None
    last_status: str
    last_message: str
    last_ip_results: list | None = None
    last_latency_ms: int | None
    last_value: float | None
    last_checked_at: datetime | None
    snooze_until: datetime | None = None
    created_at: datetime
    updated_at: datetime
    uptime_24h: float | None = None  # % за 24ч (заполняется в overview)
    beats: list[str] | None = None  # последние N снимков (up/degraded/down) для мини-ленты
    # локации, из которых сайт сейчас не отвечает (пусто = отовсюду доступен).
    # Основная проверка при этом обычно зелёная — см. partial в обзоре.
    loc_down: list[str] = []
    alert_mutes: list[str] | None = None  # заглушённые типы алертов монитора


class CheckSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    status: str
    latency_ms: int | None
    value: float | None
    message: str


class CheckHistoryOut(BaseModel):
    check_id: int
    interval_seconds: int
    points: list[CheckSampleOut]


class UptimeOut(BaseModel):
    day: float | None
    week: float | None
    month: float | None


class LocationHealth(BaseModel):
    id: int
    name: str
    down: int   # мониторов не отвечает из этой точки
    total: int  # сколько всего через неё проверяется


class ChecksOverviewOut(BaseModel):
    total: int
    up: int
    degraded: int
    down: int
    unknown: int
    disabled: int = 0  # выключенные — отдельно, в up/down/… не входят
    # Доступен с основной проверки, но НЕ доступен из части локаций. В up/down
    # не входит: по основному статусу это «работает», и раньше такой сайт нигде
    # в счётчиках не всплывал — «Недоступно: 1», хотя проблем две.
    partial: int = 0
    # Сводка по точкам проверки: сколько мониторов из каждой не отвечает.
    # Если из одной локации «падает» сразу многое — почти наверняка сломана она
    # сама, а не все эти сайты разом.
    loc_summary: list[LocationHealth] = []
    open_incidents: int
    checks: list[CheckOut]


class CheckIncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    check_id: int
    check_name: str = ""
    status: str
    started_at: datetime
    ended_at: datetime | None
    last_message: str
    # ушёл ли по инциденту алерт. False — сбой был короче порога «N проверок подряд»:
    # самый частый вопрос «сайт лежал, а алерта нет», и ответ на него должен быть
    # виден прямо в карточке, а не выясняться по базе
    notified: bool = False
    # порог этого монитора в человеческом виде — чтобы объяснить, чего не хватило
    alert_after: int = 0
    interval_seconds: int = 0


# --- алерты / тихие часы ---


class AlertConfigOut(BaseModel):
    telegram_token: str
    telegram_chat: str
    telegram_api: str
    webhook: str
    flood_threshold: int  # ≥N алертов за цикл → один дайджест (0 = выкл)
    enabled: bool
    muted: bool  # временная пауза всех алертов


class AlertConfigIn(BaseModel):
    telegram_token: str = Field(default="", max_length=256)
    telegram_chat: str = Field(default="", max_length=64)
    telegram_api: str = Field(default="", max_length=256)
    webhook: str = Field(default="", max_length=512)
    flood_threshold: int = Field(default=6, ge=0, le=1000)
    muted: bool = False


class AlertTestResult(BaseModel):
    sent: bool
    errors: list[str] = []


class ServerAlertRule(BaseModel):
    enabled: bool = True
    text: str = Field(default="", max_length=500)
    scope_type: str = "all"  # all | groups | servers
    scope: list = []  # имена групп или id серверов


class ServerAlertKindInfo(BaseModel):
    key: str
    label: str
    default_text: str


class ServerAlertRulesOut(BaseModel):
    rules: dict[str, ServerAlertRule]
    kinds: list[ServerAlertKindInfo]


class ServerAlertRulesIn(BaseModel):
    rules: dict[str, ServerAlertRule]


class RetentionConfig(BaseModel):
    """Сроки хранения тайм-серий, дней."""

    server_days: int = Field(ge=1, le=3650)  # метрики серверов
    sample_days: int = Field(ge=1, le=3650)  # история проверок сайтов


class BackupConfig(BaseModel):
    """Автобэкап: интервал (часы, 0 = выкл) и сколько файлов хранить."""

    interval_hours: int = Field(ge=0, le=24 * 30)
    keep: int = Field(ge=1, le=365)


class BackupFileInfo(BaseModel):
    name: str
    size: int
    created_at: str


class RestoreResult(BaseModel):
    restored: dict[str, int]


# --- локации (прокси) ---


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # http://user:pass@host:port | https://… | socks5://host:port | "" = напрямую
    url: str = Field(default="", max_length=512)
    enabled: bool = True


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None


class LocationTest(BaseModel):
    url: str = Field(default="", max_length=512)


class ProxyTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    enabled: bool
    created_at: datetime


# --- серверы (агент-push) ---


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    group_name: str = Field(default="", max_length=64)
    # IP, с которого агент будет ходить в панель (опционально, для фаервола)
    agent_ip: str = Field(default="", max_length=64)
    cpu_alert_percent: int = Field(default=90, ge=0, le=100)
    mem_alert_percent: int = Field(default=90, ge=0, le=100)
    disk_alert_percent: int = Field(default=90, ge=0, le=100)
    disk_warn_percent: int = Field(default=85, ge=0, le=100)
    disk_crit_percent: int = Field(default=95, ge=0, le=100)
    offline_after_seconds: int = Field(default=120, ge=30, le=86400)
    alert_sustain_seconds: int = Field(default=900, ge=0, le=86400)


class ServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    group_name: str | None = Field(default=None, max_length=64)
    agent_ip: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    cpu_alert_percent: int | None = Field(default=None, ge=0, le=100)
    mem_alert_percent: int | None = Field(default=None, ge=0, le=100)
    disk_alert_percent: int | None = Field(default=None, ge=0, le=100)
    disk_warn_percent: int | None = Field(default=None, ge=0, le=100)
    disk_crit_percent: int | None = Field(default=None, ge=0, le=100)
    temp_alert_c: int | None = Field(default=None, ge=0, le=120)
    conntrack_alert_percent: int | None = Field(default=None, ge=0, le=100)
    # порог глубины очереди RabbitMQ (0 = алерты по очередям на ноде выключены)
    queue_alert_depth: int | None = Field(default=None, ge=0, le=10_000_000)
    # переопределения по очередям: {"<источник>|<vhost>/<имя>": порог}, 0 = не алертить
    queue_alert_over: dict | None = None
    disk_temp_alert_c: int | None = Field(default=None, ge=0, le=120)
    alert_mutes: list[str] | None = None  # типы, заглушённые для этого сервера
    backup_not_required: bool | None = None  # «бэкап на сервере не требуется» (снять алерт)
    db_dumps_ok: bool | None = None  # «дампы СУБД настроены отдельно»
    backup_deadline_hour: int | None = Field(default=None, ge=0, le=23)  # окно бэкапа: до часа
    backup_anytime: bool | None = None  # бэкап в любое время (не уведомлять о дневном)
    offline_after_seconds: int | None = Field(default=None, ge=30, le=86400)
    alert_sustain_seconds: int | None = Field(default=None, ge=0, le=86400)


class BackupAudit(BaseModel):
    """Находка аудита покрытия бэкапа: что на ноде рискует не восстановиться."""

    kind: str  # mount / bind / db
    subject: str  # путь или имя контейнера
    detail: str  # человекочитаемое пояснение
    gap: bool  # True = данных нет в бэкапе; False = есть, но восстановимость под вопросом
    # для kind=db: код движка (pg/mysql/ch) — пусто, если движок дампить не умеем.
    # can_dump=True → панель снимет дамп сама (docker/локально); False → только предложит
    # манифест CronJob (под kubernetes: нужен exec, которого у агента намеренно нет).
    dump_engine: str = ""
    can_dump: bool = False
    # непусто → включение дампа стоит простоя (Neo4j Community умеет дамп только с
    # остановленной базы). Показываем в UI до включения, а не постфактум в графиках.
    downtime: str = ""
    container: str = ""
    pods: list[str] = []  # ns/name подов с этой СУБД (для генерации манифеста)
    # ключ для точечного приглушения ("db:RabbitMQ") и признак «приглушена». Приглушённая
    # находка НЕ исчезает, а уезжает в свёрнутый список: иначе через месяц не вспомнить,
    # что спрятал её сам, и легко решить, что панель перестала проверять.
    key: str = ""
    muted: bool = False
    # конкретный экземпляр движка (имя контейнера). Пусто = под/нативная установка.
    # Входит в ключ: две postgres на ноде глушатся и дампятся независимо.
    instance: str = ""


class BackupAuditMuteIn(BaseModel):
    """Приглушить/вернуть одну находку аудита покрытия (ключ вида "db:RabbitMQ")."""

    key: str = Field(min_length=1, max_length=200)
    muted: bool


class BackupServerDeployIn(BaseModel):
    """Развернуть rest-server на ноде с нуля. Образ и режим (--append-only --private-repos)
    зашиты в helper и панелью НЕ управляются — иначе это вектор ослабления защиты."""

    port: int = Field(64100, ge=1024, le=65535)
    tls: bool = True  # поднять self-signed TLS-фронт на 64101


class HelperAdvice(BaseModel):
    """Устаревший setup-скрипт (helper) на ноде: имя, метка, установленная и текущая версии.
    Команду переустановки собирает фронт из origin панели."""

    name: str  # backup-setup / backupserver-setup / kube-setup
    label: str  # человекочитаемо
    # версии — строки «мажор.минор» (0.12). Строкой, а не числом: 0.13 новее 0.2,
    # как дробь это неверно (см. _ver_key в servers.py)
    installed: str | None = None  # None = helper до версионирования
    current: str


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    group_name: str
    enabled: bool
    hostname: str
    os: str
    agent_version: str
    target_agent_version: str  # если задана и != agent_version — идёт обновление
    local_ip: str
    external_ip: str
    agent_ip: str = ""
    # ISO-код страны по IP (офлайн-таблица, см. app/geoip.py); '' = не определилась
    country: str = ""
    # контейнеры, по которым алерт уже ушёл: {имя: "down"|"loop"} — панель подсвечивает
    # ровно их, а не всё подряд с ненулевым RestartCount
    docker_alerts: dict[str, str] = Field(default_factory=dict)
    last_report: dict | None
    last_seen: datetime | None
    snooze_until: datetime | None = None
    alert_snoozes: dict[str, datetime] | None = None  # {тип: до} — точечный снуз
    online: bool = False  # вычисляется (last_seen в пределах offline_after)
    cpu_alert_percent: int
    mem_alert_percent: int
    disk_alert_percent: int
    disk_warn_percent: int
    disk_crit_percent: int
    temp_alert_c: int
    conntrack_alert_percent: int
    queue_alert_depth: int = 0
    # None, а не dict: в БД колонка nullable, и пустое значение приходит как NULL —
    # обязательный dict валил ServerOut пятисоткой на всём списке серверов
    queue_alert_over: dict | None = None
    disk_temp_alert_c: int
    alert_mutes: list[str] | None = None
    backup_repo_mutes: list[str] | None = None  # заглушённые репо бэкап-сервера (по имени)
    backup_audit_mutes: list[str] | None = None  # приглушённые находки покрытия (по ключу)
    backup_not_required: bool = False  # у сервера бэкап не требуется (алерт снят)
    db_dumps_ok: bool = False  # дампы СУБД настроены отдельно (пункт снят с главной)
    backup_deadline_hour: int = 8  # окно бэкапа: до какого часа должен закончиться
    backup_anytime: bool = False  # бэкап в любое время — не уведомлять о выходе за окно
    offline_after_seconds: int
    alert_sustain_seconds: int = 900
    # самодиагностика: чего агенту не хватает в systemd-юните + команда-фикс (или пусто)
    agent_advice: list[str] = []
    agent_fix_command: str | None = None
    # устаревшие setup-скрипты (helper'ы) на ноде → переустановить на детали сервера
    helper_advice: list[HelperAdvice] = []
    # аудит покрытия бэкапа: что рискует не восстановиться (показ, БЕЗ алертов)
    backup_audit: list[BackupAudit] = []


class BackupRepoMuteIn(BaseModel):
    """Заглушить/включить конкретный репозиторий бэкап-сервера (по имени)."""

    repo: str = Field(min_length=1, max_length=253)
    muted: bool


class ServerEnrollOut(BaseModel):
    server: ServerOut
    token: str  # сырой токен — показывается ОДИН раз
    install_cmd: str  # one-liner установки агента


class ServerMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    cpu_percent: float | None
    mem_percent: float | None
    disk_percent: float | None
    load1: float | None
    net_rx: float | None
    net_tx: float | None
    disk_read: float | None = None
    disk_write: float | None = None
    disk_read_iops: float | None = None
    disk_write_iops: float | None = None
    cpu_user: float | None
    cpu_system: float | None
    cpu_iowait: float | None
    cpu_irq: float | None
    cpu_cores_pct: list[float] | None = None
    cpu_freq: float | None = None
    cpu_temp: float | None = None
    cpu_throttle: float | None = None
    oom_kill: float | None = None
    mem_cache: float | None
    mem_free: float | None
    swap_in: float | None = None  # байт/сек
    swap_out: float | None = None
    mem_slab: float | None = None  # байты
    mem_dirty: float | None = None
    mem_writeback: float | None = None
    net_ifaces: list[dict] | None = None  # [{"if","rx","tx"}]
    disk_devs: list[dict] | None = None  # [{"dev","util","await","temp"}]
    conntrack_count: float | None = None
    conntrack_max: float | None = None
    sock_used: float | None = None
    sock_tcp: float | None = None
    sock_tcp_tw: float | None = None
    sock_udp: float | None = None
    disks: list[dict] | None = None  # [{"mount": "/", "pct": 53.0}, …]


class OomEventOut(BaseModel):
    """Строка журнала OOM-киллов: когда, кого убило, сколько за интервал."""

    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    victim: str
    count: int


# --- агент → панель ---


class AgentReportIn(BaseModel):
    hostname: str = Field(default="", max_length=255)
    os: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=32)
    cpu_model: str = Field(default="", max_length=128)
    is_vm: bool = False
    virt: str = Field(default="", max_length=64)
    local_ip: str = Field(default="", max_length=64)
    uptime_seconds: int = 0
    cpu_percent: float = 0.0
    mem_used: int = 0
    mem_total: int = 0
    swap_used: int = 0
    swap_total: int = 0
    load: list[float] = []  # [1м, 5м, 15м]
    disks: list[dict] = []  # [{"mount","used","total"}]
    # СУБД, найденные сканом процессов (нативные + в контейнерах + в подах). Поле верхнего
    # уровня, поэтому его ОБЯЗАТЕЛЬНО объявлять здесь: незадекларированное pydantic молча
    # выбрасывает, и в last_report оно бы не доехало.
    db_engines: list[str] = []
    # прикладные метрики сервисов (очереди RabbitMQ и т.п.) — читаются без секретов
    services: list[dict] = []
    # веб-серверы/прокси (nginx/envoy/traefik/…) + сайты, которые обслуживают. Тоже верхний
    # уровень → обязателен здесь, иначе pydantic выбросит и в last_report не доедет.
    web_services: list[dict] = []
    db_stats: list[dict] = []  # инвентарь СУБД: базы/размеры/логины (хелпер dbstat-setup)
    net_rx: float = 0.0  # байт/сек (агент считает по дельте)
    net_tx: float = 0.0
    disk_read: float = 0.0  # байт/сек
    disk_write: float = 0.0
    disk_read_iops: float = 0.0  # операций/сек
    disk_write_iops: float = 0.0
    cpu_cores: int = 0
    cpu_user: float = 0.0
    cpu_system: float = 0.0
    cpu_iowait: float = 0.0
    cpu_irq: float = 0.0
    cpu_cores_pct: list[float] = []
    cpu_freq: float | None = None
    cpu_temp: float | None = None
    cpu_throttle: float | None = None
    oom_kill: float | None = None
    oom_victim: str = Field(default="", max_length=200)
    mem_cached: int = 0
    mem_free: int = 0
    swap_in: float = 0.0  # байт/сек
    swap_out: float = 0.0
    mem_slab: int = 0  # байты
    mem_dirty: int = 0
    mem_writeback: int = 0
    net_ifaces: list[dict] = []  # [{"if","rx","tx"}] — байт/сек по интерфейсам
    disk_devs: list[dict] = []  # [{"dev","util","await","temp"}] — % / мс / °C по устройствам
    top_cpu: list[dict] = []  # [{"pid","comm","cpu","rss"}] — снапшот, в last_report
    top_mem: list[dict] = []
    conntrack_count: float = 0.0
    conntrack_max: float = 0.0
    sock_used: float = 0.0
    sock_tcp: float = 0.0
    sock_tcp_tw: float = 0.0
    sock_udp: float = 0.0
    checks: list[dict] = []  # [{"key","type","status","message"}]
    caps: dict[str, bool] = {}  # самодиагностика прав из юнита ({"kmsg": true/false})
    docker: dict | None = None  # снимок Docker: {present,access,version,compose,containers}
    kube: dict | None = None  # снимок Kubernetes: {present,access,flavor,version,nodes,pods,…}
    backup: dict | None = None  # статус restic-бэкапа: {present,success,last_backup_ts,skipped,…}
    backup_server: dict | None = None  # rest-server: {present,running,version,repos:[{name,valid,…}]}
    setup_versions: dict | None = None  # версии setup-скриптов на ноде: {backup-setup:1, kube-setup:1,…}
    clock: dict | None = None  # статус синхронизации времени: {synced,ntp,service}
    clock_unix: int = 0  # локальные часы ноды на момент отправки (для расчёта сдвига панелью)


class AgentConfigOut(BaseModel):
    """Ответ агенту: период и структурные проверки (панель рулит, канал — исходящий)."""

    interval: int = 15
    checks: list[dict] = []
    # != None → панель просит агент обновиться до version. Агент ставит ТОЛЬКО
    # подписанный и более новый бинарь (проверяет подпись манифеста сам).
    update: dict | None = None
    # docker-команды на исполнение через read-only proxy: [{id,container,action,tail}]
    docker_commands: list[dict] = []
    # kube-команды: [{id,ns,kind,name,action,tail,since}] (rollout_restart/delete_pod/logs)
    kube_commands: list[dict] = []
    # backup-команды: [{id,action,mode,paths,schedule}] (set_paths/set_schedule/run_now)
    backup_commands: list[dict] = []


class DockerCommandIn(BaseModel):
    """Панель → очередь: действие над контейнером сервера."""

    container: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(restart|stop|start|logs)$")
    tail: int = Field(default=200, ge=1, le=20000)  # строк для logs (если since=0)
    since: int = Field(default=0, ge=0, le=30 * 86400)  # logs за последние N сек (0=tail)


class DockerCommandOut(BaseModel):
    """Статус/результат команды (панель поллит после постановки)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    container: str
    action: str
    status: str  # pending/running/done/error
    ok: bool | None
    result: str


class DockerResultIn(BaseModel):
    """Агент → панель: результат исполнения команды."""

    id: int
    ok: bool
    output: str = Field(default="", max_length=22_000_000)  # логи за день бывают крупными


# k8s DNS-1123: строго валидируем имена/namespace (агент валидирует повторно)
_K8S_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"


class KubeCommandIn(BaseModel):
    """Панель → очередь: управляющее действие над ресурсом кластера сервера.
    Только белый список действий: rollout_restart (deploy/sts/ds), delete_pod, logs."""

    ns: str = Field(min_length=1, max_length=253, pattern=_K8S_NAME)
    kind: str = Field(default="pod", pattern="^(deployment|statefulset|daemonset|pod)$")
    name: str = Field(min_length=1, max_length=253, pattern=_K8S_NAME)
    action: str = Field(pattern="^(rollout_restart|delete_pod|logs)$")
    tail: int = Field(default=400, ge=1, le=20000)
    since: int = Field(default=0, ge=0, le=30 * 86400)


class KubeCommandOut(BaseModel):
    """Статус/результат kube-команды (панель поллит после постановки)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ns: str
    kind: str
    name: str
    action: str
    status: str  # pending/running/done/error
    ok: bool | None
    result: str


class KubeResultIn(BaseModel):
    """Агент → панель: результат исполнения kube-команды."""

    id: int
    ok: bool
    output: str = Field(default="", max_length=22_000_000)


# путь бэкапа: абсолютный, безопасные символы, без «..» (агент+helper валидируют повторно)
_BACKUP_PATH = r"^/[A-Za-z0-9._/+-]+$"  # '+' — для /lost+found (стандартный exclude ext-ФС)


class BackupCommandIn(BaseModel):
    """Панель → очередь: управление restic-бэкапом ноды (через узкий helper).
    Только белый список: set_paths (include/exclude), set_schedule, run_now."""

    action: str = Field(pattern="^(set_paths|set_schedule|run_now|dump_setup|dump_remove|restic_update|update_image|timesync)$")
    mode: str = Field(default="exclude", pattern="^(include|exclude)$")
    paths: list[str] = Field(default_factory=list, max_length=200)
    schedule: str = Field(default="", pattern=r"^$|^([01][0-9]|2[0-3]):[0-5][0-9]$")
    # dump_setup: локальные дампы СУБД перед файловым бэкапом
    # Список ДОЛЖЕН совпадать с _DUMP_ENGINE в api/servers.py и с разбором в
    # agent/backup-setup.sh: забытый движок молча отваливался бы 422-й на кнопке
    # «включить дампы» (так уже вышло с grafana).
    engine: str = Field(default="", pattern="^$|^(pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j)$")
    container: str = Field(default="", max_length=64, pattern=r"^$|^[A-Za-z0-9._-]+$")
    # настройки дампа: каталог, сколько последних хранить, минимум свободного места (%).
    # Пусто/0 у dump_dir → helper берёт дефолт /backup. dump_keep 1..30, dump_minfree 0..50.
    dump_dir: str = Field(default="", max_length=200)
    dump_keep: int = Field(default=0, ge=0, le=30)
    dump_minfree: int = Field(default=10, ge=0, le=50)

    @field_validator("paths")
    @classmethod
    def _paths_ok(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(_BACKUP_PATH, p) or ".." in p:
                raise ValueError(f"недопустимый путь: {p}")
        return v

    @field_validator("dump_dir")
    @classmethod
    def _dump_dir_ok(cls, v: str) -> str:
        # уходит в rm/mkdir от root на ноде: только абсолютный безопасный путь. Пусто = дефолт.
        if v and (not re.match(_BACKUP_PATH, v) or ".." in v or v == "/"):
            raise ValueError(f"недопустимый каталог дампов: {v}")
        return v


class BackupCommandOut(BaseModel):
    """Статус/результат backup-команды (панель поллит после постановки)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    action: str
    status: str  # pending/running/done/error
    ok: bool | None
    result: str


class BackupResultIn(BaseModel):
    """Агент → панель: результат backup-команды."""

    id: int
    ok: bool
    output: str = Field(default="", max_length=100_000)


class BackupSetupIn(BaseModel):
    """Панель → оркестрация: настроить restic-бэкап ноды на выбранный бэкап-сервер.
    Секреты (repopass/hpass) генерит бэкенд и НЕ хранит; курьерит через спул агентам."""

    backup_server_id: int
    mode: str = Field(default="exclude", pattern="^(include|exclude)$")
    paths: list[str] = Field(default_factory=list, max_length=200)
    schedule: str = Field(default="23:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    # все keep >= 1: всегда держим минимум 1 последний/дневной/недельный/месячный (нулей нет)
    keep_last: int = Field(default=3, ge=1, le=3650)
    keep_daily: int = Field(default=7, ge=1, le=3650)
    keep_weekly: int = Field(default=4, ge=1, le=3650)
    keep_monthly: int = Field(default=6, ge=1, le=3650)
    tls: bool = True  # True → self-signed caddy :64101 + --cacert; False → HTTP как в ансибл-роли

    @field_validator("paths")
    @classmethod
    def _paths_ok(cls, v: list[str]) -> list[str]:
        for p in v:
            if not re.match(_BACKUP_PATH, p) or ".." in p:
                raise ValueError(f"недопустимый путь: {p}")
        return v


class BackupCredsOut(BaseModel):
    """Данные для восстановления (repo URL + пароль). Достаются с ноды/бэкап-сервера по
    запросу админа, в БД НЕ хранятся. Показывать с предупреждением."""

    repo_url: str = ""
    repopass: str = ""
    cacert_file: str = ""       # ПУТЬ к серту на ноде (для команд, что бегут на ней же)
    cacert_pem: str = ""        # САМ серт: без него доступ бесполезен на чужой машине
    repo_local: str = ""       # локальный путь репо на бэкап-сервере (для fallback-restore)
    source: str = "client"     # откуда достали: client / backup-server
    server_name: str = ""      # имя бэкап-сервера (для fallback)


class BackupSetupJobOut(BaseModel):
    """Статус фоновой оркестрации «настроить бэкап» (фронт поллит)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str  # running/done/error
    steps: list[dict] = Field(default_factory=list)  # [{step, ok, detail}]
    message: str = ""


class AgentReleaseOut(BaseModel):
    """Доступная (подписанная) версия агента + текущие версии setup-скриптов (панель
    сверяет с установленными на нодах и флагует устаревшие helper'ы)."""

    version: str  # пусто, если релиз-манифест не найден
    setup_versions: dict = Field(default_factory=dict)  # {backup-setup:1, kube-setup:1, …}
    problem: str = ""  # раздаваемый бинарь разошёлся с подписанным манифестом


class AgentUpdateReq(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    server_ids: list[int] | None = None  # None = все включённые (иначе canary/подмножество)


class AgentUpdateCancel(BaseModel):
    server_ids: list[int] | None = None  # None = снять со всех


class LocationResultOut(BaseModel):
    """Результат монитора из локации (+ имя/enabled локации для UI)."""

    location_id: int
    name: str = ""
    enabled: bool = True
    direct: bool = False  # прямая локация (без прокси) = основная проверка панели
    status: str
    latency_ms: int | None
    message: str
    checked_at: datetime
