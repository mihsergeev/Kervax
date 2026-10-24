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


def _clean_paths(v: list[str] | None) -> list[str] | None:
    """Additional paths of a monitor: /health, /api/status?full=1. Empty lines are dropped,
    repeats are merged, the order is kept."""
    if v is None:
        return None
    out: list[str] = []
    for raw in v:
        path = (raw or "").strip()
        if not path:
            continue
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"путь должен начинаться с /: {path[:60]}")
        if any(c.isspace() for c in path) or len(path) > 256:
            raise ValueError(f"недопустимый путь: {path[:60]}")
        if path not in out:
            out.append(path)
    if len(out) > 10:
        raise ValueError("дополнительных путей не больше 10")
    return out


CheckType = Literal["http", "tcp_port", "cert", "push", "dns"]


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
    # учетке только для просмотра разрешено читать логи (см. модель User)
    can_read_logs: bool = False


class MeOut(UserOut):
    """/auth/me: во время входа админа под этой учеткой - кто вошел и до какого времени
    (плашка "Вы вошли как ..." и кнопка "Вернуться к себе")."""

    impersonator: str | None = None
    imp_id: int | None = None
    imp_expires: datetime | None = None


class ImpersonateOut(BaseModel):
    access_token: str
    imp_id: int
    username: str
    expires_at: datetime


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=128)
    # роль новой учётки; по умолчанию — только просмотр
    role: str = Field(default="viewer", pattern="^(admin|editor|viewer)$")
    # пусто = без ограничений (все разделы / все группы)
    sections: list[str] = Field(default_factory=list, max_length=20)
    server_groups: list[str] = Field(default_factory=list, max_length=100)
    site_groups: list[str] = Field(default_factory=list, max_length=100)
    # чтение логов учеткой только для просмотра; у других ролей не хранится
    can_read_logs: bool = False


class UserUpdate(BaseModel):
    """Правка учётки админом: роль и границы доступа. Пароль — отдельным маршрутом."""

    role: str | None = Field(default=None, pattern="^(admin|editor|viewer)$")
    sections: list[str] | None = Field(default=None, max_length=20)
    server_groups: list[str] | None = Field(default=None, max_length=100)
    site_groups: list[str] | None = Field(default=None, max_length=100)
    can_read_logs: bool | None = None


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
    can_read_logs: bool = False
    totp_enabled: bool
    created_at: datetime


class UserCardOut(UserListItem):
    """Учетка для окна "Пользователи": к роли и области - вход и действия из журнала."""

    tg_linked: bool = False  # личные алерты в Telegram привязаны
    last_login: datetime | None = None
    last_ip: str = ""
    fails_24h: int = 0  # неудачные входы под этим логином за сутки
    actions_30d: int = 0  # действия за 30 дней, без входов


class UsersOverview(BaseModel):
    users: list[UserCardOut]
    audit: list["AuditEntryOut"]  # журнал действий, новые сверху
    fails_24h: int  # все неудачные входы за сутки, и под несуществующими логинами
    fail_ips_24h: int  # с скольких адресов


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
    # админ действовал под этой учеткой (вход под чужой учеткой), пусто - от себя
    as_user: str = ""


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
    # проверять изнутри сервера (сайт закрыт белым списком снаружи)
    probe_local: bool = False
    check_all_ips: bool = False
    check_ssl: bool = True
    check_domain: bool = True
    # алерт за неделю и за день; за месяц срок только виден в списке сайтов и карточке
    ssl_warn_days: list[int] = [7, 1]
    domain_warn_days: list[int] = [7, 1]
    check_locations: bool = False  # локации опциональны — по умолчанию не через прокси
    location_ids: list[int] | None = None
    alert_mutes: list[str] | None = None  # типы алертов, заглушённые для этого монитора

    # additional paths of the same site, checked with the main address (see checks.extra_paths_of)
    extra_paths: list[str] = []
    # push: сигнал ждем каждые push_period секунд, плюс допуск (0 = десятая часть, от 2 минут).
    # 0 пускаем: так период лежит у всех мониторов, кроме push, и форма правки шлет его обратно.
    # Push-монитору минуту и час по умолчанию подставляет checks._push_defaults
    push_period: int = Field(default=3600, ge=0, le=31 * 86400)
    push_grace: int = Field(default=0, ge=0, le=7 * 86400)
    # dns: тип записи, ожидаемые значения по строке, equal | contains, IP резолвера
    dns_type: Literal["", "A", "AAAA", "CNAME", "MX", "NS", "TXT"] = ""
    dns_expect: str = Field(default="", max_length=4000)
    dns_match: Literal["equal", "contains"] = "equal"
    dns_server: str = Field(default="", max_length=64)
    # от какого сервера зависит (app/parents.py): auto, server (parent_server_id) или off
    parent_mode: Literal["auto", "server", "off"] = "auto"
    parent_server_id: int | None = None

    _clean = field_validator("ssl_warn_days", "domain_warn_days")(_clean_warn_days)
    _paths = field_validator("extra_paths")(_clean_paths)


class CheckTryIn(BaseModel):
    """Пробная проверка из формы нового монитора (app/try_probe.py): только то, что влияет
    на сам запрос. part - какую часть проверить: форма спрашивает их одновременно."""

    part: Literal["panel", "expiry", "locations", "inside"]
    # правка монитора: сам он дублем себе не считается
    check_id: int | None = None
    type: Literal["http", "tcp_port", "dns"]
    target: str = Field(min_length=1, max_length=512)
    port: int = Field(default=0, ge=0, le=65535)
    timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    degraded_ms: int = Field(default=2000, ge=1, le=60000)
    method: str = Field(default="GET", pattern=_METHOD)
    expected_status: str = Field(default="200-399", max_length=32)
    keyword_up: str = Field(default="", max_length=256)
    keyword_down: str = Field(default="", max_length=256)
    auth_method: str = Field(default="", pattern="^(|basic)$")
    auth_user: str = Field(default="", max_length=128)
    auth_pass: str = Field(default="", max_length=256)
    http_headers: str = Field(default="", max_length=4096)
    ignore_tls: bool = False
    extra_paths: list[str] = []
    # dns: какую запись спросить, у какого резолвера и что ждать
    dns_type: Literal["", "A", "AAAA", "CNAME", "MX", "NS", "TXT"] = ""
    dns_server: str = Field(default="", max_length=64)
    dns_expect: str = Field(default="", max_length=4000)
    dns_match: Literal["equal", "contains"] = "equal"

    _paths = field_validator("extra_paths")(_clean_paths)


class TryResult(BaseModel):
    status: str  # up | degraded | down | pending | none
    latency_ms: int | None = None
    message: str = ""
    # вид беды одним словом и HTTP-код (domain_probe._kind); для inside еще no_server,
    # offline, no_answer
    kind: str = ""
    code: int = 0


class TryPanel(TryResult):
    ip_results: list | None = None
    path_results: list | None = None
    # dns: что ответил резолвер, и с чем сравнили: "new" - запомненного ответа еще нет
    # (монитор запомнит этот), "kept" - сравнили с тем, что монитор уже запомнил
    answers: list | None = None
    baseline: str = ""


class TryLocation(TryResult):
    id: int
    name: str


class TryInside(TryResult):
    server_id: int | None = None
    server_name: str = ""
    web: str = ""  # какой веб-сервер держит домен: nginx, caddy, ingress...
    request_id: int | None = None
    fast: bool = False
    deadline: int = 0


class CheckTryOut(BaseModel):
    panel: TryPanel | None = None
    ssl_days: int | None = None
    ssl_message: str = ""
    domain_days: int | None = None
    domain_message: str = ""
    locations: list[TryLocation] | None = None
    inside: TryInside | None = None
    # с частью panel: группа мониторов того же домена и монитор, который уже смотрит
    # этот адрес (чтобы не завести второй такой же)
    group: str = ""
    existing_id: int | None = None
    existing_name: str = ""


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
    push_period: int | None = Field(default=None, ge=0, le=31 * 86400)
    push_grace: int | None = Field(default=None, ge=0, le=7 * 86400)
    dns_type: Literal["", "A", "AAAA", "CNAME", "MX", "NS", "TXT"] | None = None
    dns_expect: str | None = Field(default=None, max_length=4000)
    dns_match: Literal["equal", "contains"] | None = None
    dns_server: str | None = Field(default=None, max_length=64)
    parent_mode: Literal["auto", "server", "off"] | None = None
    parent_server_id: int | None = None
    probe_local: bool | None = None
    check_all_ips: bool | None = None
    check_ssl: bool | None = None
    check_domain: bool | None = None
    ssl_warn_days: list[int] | None = None
    domain_warn_days: list[int] | None = None
    check_locations: bool | None = None
    location_ids: list[int] | None = None
    alert_mutes: list[str] | None = None

    extra_paths: list[str] | None = None

    _clean = field_validator("ssl_warn_days", "domain_warn_days")(_clean_warn_days)
    _paths = field_validator("extra_paths")(_clean_paths)


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
    # Сайтов за белым списком обычно не один, а десяток сразу — включать по одному
    # мучительно. Ноду для каждого панель найдёт сама (см. rebind_local_probes).
    probe_local: bool | None = None
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

    # "disk" или "disk@2" - тип, опционально с макс. заглушаемым уровнем (см. _muted);
    # web_5xx:<ключ лога> - 5xx одного лога (домена), остальные логи ноды алертят;
    # unit:<юнит> - один упавший юнит systemd, остальные алертят;
    # qstuck:<ключ очереди> - одна очередь (ход и порог глубины);
    # qfail:<источник брокера> - упавшие задачи одного брокера очередей;
    # worker:<ключ воркера> - один воркер под supervisor или pm2;
    # cron:<где>|<команда> - одна задача cron;
    # logerr:<где>|<контейнер пода> - всплеск ошибок в одном логе;
    # fpm:<где>|<пул> - один пул PHP-FPM;
    # gitlab:<хост или контейнер> - один GitLab;
    # oom:<виновник> - OOM из-за одной службы, контейнера или процесса (app/oom.py culprit)
    kind: str = Field(min_length=1, max_length=480,
                      pattern=r"^(?:[a-z_][a-z0-9_]*(?:@[1-3])?|web_5xx:[^\x00-\x1f]{1,220}|unit:[A-Za-z0-9@._:\\-]{1,200}"
                              r"|qstuck:[^\x00-\x1f]{1,470}|qfail:[^\x00-\x1f]{1,470}|worker:[^\x00-\x1f]{1,470}|cron:[^\x00-\x1f]{1,470}|logerr:[^\x00-\x1f]{1,470}|fpm:[^\x00-\x1f]{1,470}|gitlab:[^\x00-\x1f]{1,470}"
                              r"|oom:[^\x00-\x1f]{1,470})$")
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
    # Что решил автоанализ картинки при загрузке. Считает браузер: только он видит
    # пиксели, а тащить на бэкенд декодер картинок ради этого не стоит.
    # plate_auto — плашка нужна на ТЁМНОЙ теме (у картинки свой фон или она тёмная).
    plate_auto: bool = False
    # На СВЕТЛОЙ теме плашка нужна только картинке со своим фоном: тёмный логотип на
    # прозрачном фоне там и так виден. None — логотип залит до разделения по темам,
    # анализа нет, и браузер делает его сам при показе.
    plate_light: bool | None = None
    # отдельный вариант для тёмной темы (обычно светлая версия логотипа)
    dark_logo: bool = False
    dark_plate: bool = False  # у варианта для тёмной темы свой фон — нужна плашка
    version: int        # растёт при замене; используется как ?v= для кэша


class BrandingIn(BaseModel):
    # base64/data-URL; пусто — файл не меняем, сохраняем только настройки
    data: str = Field(default="", max_length=1_400_000)
    plate: str = Field(default="auto", pattern="^(auto|always|never)$")
    plate_auto: bool = False  # вердикт автоанализа на стороне браузера
    plate_light: bool | None = None
    title: str = Field(default="", max_length=64)
    # вариант для тёмной темы: пусто — не менять; dark_remove — убрать
    dark: str = Field(default="", max_length=1_400_000)
    dark_plate: bool = False
    dark_remove: bool = False


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


class MuteWarning(BaseModel):
    """Объект, по которому не сработает ни один алерт.

    Причину отдаём КОДОМ, а не готовой фразой: текст живёт в словаре интерфейса и
    переводится вместе со всем остальным, а панель двуязычная.
    """

    kind: str          # server | site
    id: int
    name: str
    group: str
    reason_code: str   # group_out | no_group | no_rules
    fixable: bool      # хватит ли кнопки — есть ли куда дописать объект


class AlertCoverageOut(BaseModel):
    items: list[MuteWarning] = []


class CoverageRef(BaseModel):
    """Ссылка на немой объект: тем же видом, каким его отдал /coverage."""

    kind: str
    id: int


class CoverageFixIn(BaseModel):
    items: list[CoverageRef] = []


class CoverageFixOut(BaseModel):
    """Что получилось: сколько правил тронуто и кто остался немым."""

    rules: int = 0
    items: list[MuteWarning] = []


class LocalProbeSuggestion(BaseModel):
    """Что панель предлагает сделать с недоступным снаружи сайтом.

    Два разных случая, и лечатся они по-разному:
      local  — сайт молчит снаружи, но его домен обслуживает известная нода;
               значит можно проверять изнутри неё;
      code   — сайт ОТВЕЧАЕТ, но кодом «не пущу» (403/401). Проверять изнутри тут
               нечего и незачем: раз отвечает — он жив, и достаточно считать этот
               код нормой. Так лечатся сайты за прокси, которое само не пускает,
               даже когда локальной проверке не за что зацепиться.
    """

    check_id: int
    name: str
    host: str
    message: str  # чем именно закончилась внешняя проверка
    kind: str = "local"
    code: int = 0  # для kind="code": какой код предлагаем считать нормой
    server_id: int = 0
    server_name: str = ""
    # почему похоже на белый список (app/allowlist_hint.py): inside_up - изнутри ноды
    # открывается, never_up - снаружи не открывался ни разу, code - стабильно 403
    reason: str = ""
    outside: str = ""  # connect | reset | code: чем кончается проверка снаружи
    since: datetime | None = None  # с какого момента снаружи не открывается


class LocalProbeSuggestionsOut(BaseModel):
    items: list[LocalProbeSuggestion] = []


class LocalProbeApplyIn(BaseModel):
    check_ids: list[int]


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
    # Какие из них проверять изнутри сервера, где домен найден (сайт закрыт снаружи).
    # Ноду здесь не передают: её панель вычисляет сама, как и для «проверять локально».
    local: list[str] = Field(default_factory=list, max_length=500)
    # Какие мониторить по типовому пути, подобранному проверкой в мастере (главная не
    # открылась, а /health или /api отвечают). Сам путь панель берет из итога проверки.
    paths: list[str] = Field(default_factory=list, max_length=500)


class AdoptItemOut(BaseModel):
    """Что стало с одним доменом из запроса — мастер показывает это списком."""

    domain: str
    check_id: int = 0  # монитор по домену; 0 — не заведён или вне видимости учётки
    local: bool = False  # проверяет агент ноды — изнутри сервера
    server: str = ""  # какая нода проверяет изнутри
    path: str = ""  # типовой путь вместо главной; пусто - главная
    expected_status: str = ""  # ожидаемые коды, если не по умолчанию (MCP: 405/406 - живой)
    # почему монитор не заведён: monitored — домен уже на мониторинге, invalid — имя не
    # годится (подробность в problem); пусто — заведён
    reason: str = ""
    problem: str = ""


class AdoptResult(BaseModel):
    created: int
    # «домен — причина», по-человечески: wildcard и мусор из server_name не мониторятся
    skipped: list[str]
    hosts: dict[str, int]  # обновлённая карта, чтобы UI перерисовал галочки без перезагрузки
    local: int = 0  # из созданных — с проверкой изнутри сервера
    group_name: str = ""  # куда легли мониторы: учётке с нарезкой — первая разрешённая группа
    items: list[AdoptItemOut] = []


class DomainProbeIn(BaseModel):
    """Разово проверить найденные домены снаружи и изнутри их сервера.

    force=false — домены со свежим итогом не перепроверяются (открытие мастера);
    force=true — перепроверить (кнопка)."""

    domains: list[str] = Field(min_length=1, max_length=300)
    force: bool = False


class PathHint(BaseModel):
    """Типовой путь, который отвечает, когда главная не открылась (domain_probe.PATHS).

    expect - ожидаемые коды для монитора, если не по умолчанию: адрес MCP-сервера на
    обычный GET отвечает 405/406, и это значит "жив"."""

    path: str
    code: int
    expect: str = ""


class DomainProbeOut(BaseModel):
    """Итог разовой проверки домена: снаружи (панелью) и изнутри сервера (агентом).

    Статусы: pending — ещё проверяется; up / degraded / down — как у монитора;
    none (только изнутри) — проверить было некому. *_kind — что не так одним словом
    (http, dns, tls, refused, timeout, reset, connect, other; изнутри ещё offline и
    no_answer): текст к нему живёт в словаре интерфейса, полная причина — в *_message."""

    domain: str
    started_at: datetime
    ext_status: str
    ext_latency_ms: int | None = None
    ext_message: str = ""
    ext_kind: str = ""
    ext_code: int = 0
    local_status: str
    local_latency_ms: int | None = None
    local_message: str = ""
    local_kind: str = ""
    local_code: int = 0
    local_server: str = ""
    # главная не открылась, а типовой путь отвечает - его и предлагаем мониторить
    ext_hint: PathHint | None = None
    local_hint: PathHint | None = None
    local_hint_pending: bool = False  # агент еще перебирает пути изнутри


class DomainProbesOut(BaseModel):
    items: list[DomainProbeOut]


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    target: str
    # push: токен адреса /api/agent/push/<токен>, период, допуск, последний сигнал
    push_token: str = ""
    push_period: int = 0
    push_grace: int = 0
    push_at: datetime | None = None
    push_fail: str = ""
    # заметка о мониторе: текст, кто и когда писал
    note: str = ""
    note_by: str = ""
    note_at: datetime | None = None
    known_issue: dict | None = None  # известная проблема монитора (app/known.py)
    # dns: что спрашиваем и что ждем; baseline - запомненный ответ, last - последний
    dns_type: str = ""
    dns_expect: str = ""
    dns_match: str = "equal"
    dns_server: str = ""
    dns_baseline: list[str] | None = None
    dns_last: list[str] | None = None
    # признаки взлома (app/tamper.py, Check.tamper): level none|one|hack, signs, summary
    tamper: dict | None = None
    parent_mode: str = "auto"
    parent_server_id: int | None = None
    # от кого монитор зависит на самом деле (вычисляется): [{id, name, why, silent}], why - local,
    # site, ip или manual. Пусто - ни от кого
    parents: list[dict] = Field(default_factory=list)
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
    probe_local: bool = False
    probe_server_name: str | None = None  # чей агент проверяет (для подсказки)
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
    # отложенные сроки: {"domain"|"ssl": {mode, by, at, days}}, mode 7/1 - до скольких дней, 0 - не следим
    expiry_snooze: dict | None = None
    check_locations: bool
    location_ids: list[int] | None
    last_status: str
    last_message: str
    last_ip_results: list | None = None
    extra_paths: list[str] = []
    last_path_results: list | None = None
    last_phases: dict | None = None
    last_inside: dict | None = None
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
    # с какого момента сайт отвечает через раз (сбои вперемешку с успешными, flaky.py)
    flaky_since: datetime | None = None
    alert_mutes: list[str] | None = None  # заглушённые типы алертов монитора


class CheckRunOut(CheckOut):
    """Монитор после «Проверить сейчас» + итог именно этой проверки.

    Итог отдельно от last_*: карточка должна показать ответ на нажатие, а last_status
    к моменту чтения мог уже переписать планировщик. У сайта, который проверяет агент,
    ответ приходит не сразу — тогда run_pending несёт номер запроса, и карточка
    спрашивает его, пока не получит вердикт или run_error."""

    run_pending: int | None = None  # номер запроса к агенту: ответ ещё в пути
    run_fast: bool = False  # агент отвечает быстрым путём (секунды, а не отчёт)
    run_status: str = ""  # up | degraded | down — когда готово
    run_message: str = ""
    run_latency_ms: int | None = None
    run_at: datetime | None = None
    run_source: str = "panel"  # panel — из панели, agent — с сервера изнутри
    run_server: str = ""  # чей агент проверял
    run_error: str = ""  # проверить не удалось (агент молчит, нода не на связи)


class CheckSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    status: str
    latency_ms: int | None
    value: float | None
    message: str
    # фазы запроса, мс (среднее по точкам бакета); None - у точек фаз нет
    ph_dns: int | None = None
    ph_connect: int | None = None
    ph_tls: int | None = None
    ph_wait: int | None = None


class CheckHistoryOut(BaseModel):
    check_id: int
    interval_seconds: int
    points: list[CheckSampleOut]
    # Ширина бина, сек. По ней карточка раскладывает ленту статуса и график на всё
    # выбранное окно: у свежего монитора два бина растягивались на всю ширину, и
    # один сбой из двадцати проверок выглядел как «полдня лежал».
    step_seconds: int = 0


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
    # Отвечает через раз: по последней проверке обычно "работает", поэтому тоже отдельно
    flaky: int = 0
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
    tg_buttons: bool = False  # кнопки под алертами и команды бота
    tz: str = ""  # часовой пояс панели ("до утра" у кнопки)
    tg_poll: dict = {}  # как идет опрос бота: status ok|conflict|error|off, error, ts, bot
    # эскалация: enabled, after_min, repeat, chat, users, kinds
    escalation: dict = {}
    # кому можно эскалировать: учетки панели и привязан ли у них Telegram
    esc_candidates: list[dict] = []
    # чаты по группам: {main: all|rest, chats: [{name, chat, server_groups, site_groups}]}
    routes: dict = {}
    # какие группы есть у серверов и сайтов - для выбора в чатах по группам
    route_groups: dict = {}
    # чаты, которые последний раз не приняли сообщение: {chat: {err, at}}
    routes_state: dict = {}


class AlertConfigIn(BaseModel):
    telegram_token: str = Field(default="", max_length=256)
    telegram_chat: str = Field(default="", max_length=64)
    telegram_api: str = Field(default="", max_length=256)
    webhook: str = Field(default="", max_length=512)
    flood_threshold: int = Field(default=6, ge=0, le=1000)
    muted: bool = False
    tg_buttons: bool = False
    tz: str = Field(default="", max_length=64)
    escalation: "EscalationIn | None" = None
    routes: "RoutesIn | None" = None


class RouteIn(BaseModel):
    name: str = Field(default="", max_length=64)
    chat: str = Field(default="", max_length=64)
    server_groups: list[str] = Field(default_factory=list, max_length=100)
    site_groups: list[str] = Field(default_factory=list, max_length=100)


class RoutesIn(BaseModel):
    main: Literal["all", "rest"] = "all"
    chats: list[RouteIn] = Field(default_factory=list, max_length=20)


class RouteTestIn(BaseModel):
    chat: str = Field(min_length=1, max_length=64)


class EscalationIn(BaseModel):
    enabled: bool = False
    after_min: int = Field(default=15, ge=5, le=720)
    repeat: int = Field(default=1, ge=1, le=5)
    chat: str = Field(default="", max_length=64)
    users: list[str] = Field(default_factory=list, max_length=50)
    kinds: list[str] = Field(default_factory=lambda: ["down", "offline"], max_length=50)


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


class AnsibleAccessOut(BaseModel):
    """Доступ ansible к списку нод: включен ли, когда выпущен токен и когда им пользовались."""

    enabled: bool
    created_at: str | None = None
    used_at: str | None = None


class AnsibleNodesOut(BaseModel):
    nodes: list["AnsibleNodeOut"]


class AnsibleTokenOut(BaseModel):
    """Новый токен ansible - показывается один раз, в базе остается только хеш."""

    token: str
    created_at: str


class AnsibleServerOut(BaseModel):
    """Нода для инвентаря ansible. Имени в инвентаре панель не знает, поэтому отдает все,
    по чему его можно найти: имя в панели, hostname ноды и её адреса."""

    name: str
    hostname: str = ""
    ips: list[str] = []
    online: bool
    enabled: bool
    # Прогон kervax_helpers.yml этой ноде что-то поменяет, и она на связи: устарел helper
    # или не включен watchdog. Ровно те ноды, что панель зовет обновить на главной.
    rollout: bool
    outdated: list[str] = []  # какие helper'ы устарели
    # что на ноде надо поправить плейбуком, кроме helper'ов: "docker_sock" - внешний прокси с
    # docker-сокетом, "proxy_old" - образ внешнего прокси старше года. Плагин инвентаря
    # делает из каждого группу kervax_<имя>
    issues: list[str] = []


class AnsibleServersOut(BaseModel):
    version: str
    servers: list[AnsibleServerOut]


class AnsibleNodeOut(BaseModel):
    """Нода инвентаря ansible для окна панели: то же, что получает плагин, и подробности -
    какие helper'ы и до какой версии обновит плейбук, что за прокси и restic в группах проблем."""

    id: int
    name: str
    hostname: str = ""
    ips: list[str] = []
    online: bool
    enabled: bool
    rollout: bool                                # в группе kervax_outdated
    outdated: list["HelperAdvice"] = []
    watchdog_off: bool = False                   # сторож агента не включен, его ставит плейбук
    issues: list[str] = []                       # docker_sock / proxy_old / restic_old
    sock: list[dict] = []                        # прокси с docker-сокетом: name, image, kind
    proxy_old: list[dict] = []                   # старые образы прокси: name, image, version, age_days
    restic: str = ""                             # restic сервера бэкапов, если старше целевого
    restic_target: str = ""


class RetentionConfig(BaseModel):
    """Сроки хранения тайм-серий, дней."""

    server_days: int = Field(ge=1, le=3650)  # метрики серверов
    sample_days: int = Field(ge=1, le=3650)  # история проверок сайтов


class RetentionPart(BaseModel):
    key: str              # таблица
    bytes: int
    rows: int | None = None


class RetentionGroup(BaseModel):
    """Данные со сроком из настроек: сколько занимают, самая старая запись, прирост в сутки."""

    id: str               # srv - метрики серверов, chk - история проверок
    days: int
    oldest: datetime | None = None
    bytes: int
    span_days: float = 0      # за сколько дней накоплено лежащее (не больше срока)
    per_day: int
    parts: list[RetentionPart]


class RetentionFixed(BaseModel):
    """Данные со своим постоянным сроком. bytes None - своего размера нет (часть другой таблицы)."""

    key: str
    days: int
    bytes: int | None = None
    rows: int | None = None


class RetentionOverview(BaseModel):
    """Окно "Хранение данных": размер базы и из чего она состоит (app/retention.py)."""

    db_bytes: int | None = None
    estimated: bool = False   # SQLite без dbstat: размеры таблиц - оценка по числу строк
    groups: list[RetentionGroup]
    fixed: list[RetentionFixed]
    other_bytes: int | None = None
    prune_every: int          # секунд между чистками старого


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


class BackupPart(BaseModel):
    key: str  # таблица в файле бэкапа: checks, servers, ...
    rows: int


class BackupOverview(BaseModel):
    """Окно "Бэкап": настройки автобэкапа, когда был последний и будет следующий, файлы
    на сервере и что входит в бэкап."""

    interval_hours: int
    keep: int
    last_auto: datetime | None = None
    next_auto: datetime | None = None
    files: list[BackupFileInfo]
    total_bytes: int
    parts: list[BackupPart]


# --- локации (прокси) ---


class LocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # http://user:pass@host:port | https://… | socks5://host:port | "" = напрямую
    url: str = Field(default="", max_length=512)
    enabled: bool = True
    monitors: bool = True  # False - только для внешней проверки портов
    # куда прокси выходит в интернет - из проверки, которую окно только что сделало
    exit_ip: str = Field(default="", max_length=64)
    exit_country: str = Field(default="", max_length=2)


class LocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    monitors: bool | None = None
    exit_ip: str | None = Field(default=None, max_length=64)
    exit_country: str | None = Field(default=None, max_length=2)


class LocationTest(BaseModel):
    url: str = Field(default="", max_length=512)


class ProxyTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int | None = None
    # куда прокси выходит в интернет: адрес и ISO-код страны, '' - не определилось
    exit_ip: str = ""
    exit_country: str = ""


# Окно локаций: как каждая проверяет сейчас и за сутки, и где какой сайт недоступен
class LocationStat(BaseModel):
    id: int
    checks: int = 0  # сколько мониторов проверяется через нее (у "напрямую" - все включенные)
    up: int = 0
    degraded: int = 0
    down: int = 0
    up_pct: float | None = None  # доля проверок за сутки без "недоступен"
    avg_ms: int | None = None
    last_at: datetime | None = None
    # все проверенные через нее сайты недоступны, а напрямую часть отвечает: сломан сам прокси
    broken: bool = False
    error: str = ""


class LocSiteCell(BaseModel):
    status: str
    message: str = ""
    fails: int = 0


class LocSiteRow(BaseModel):
    check_id: int
    name: str
    group: str = ""
    main: LocSiteCell  # основная проверка, с самой панели
    cells: dict[int, LocSiteCell] = {}  # id локации-прокси -> последний результат


class LocationsOverview(BaseModel):
    locations: list[LocationStat]
    # через какую локацию панель смотрит порты нод снаружи; None - сама панель
    ports_location_id: int | None = None
    # сайты, недоступные хоть откуда-то (сломанные прокси не в счет)
    sites: list[LocSiteRow]


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    enabled: bool
    monitors: bool = True
    exit_ip: str = ""
    exit_country: str = ""
    exit_at: datetime | None = None
    created_at: datetime


# --- серверы (агент-push) ---


def _agent_ip_ok(v):
    """IP сервера уходит в фаервол хоста панели (ops/agent-firewall-sync.sh): только одиночный
    адрес. Подсеть 0.0.0.0/0 открывала 80/443 панели всем."""
    if v is None:
        return v
    v = str(v).strip()
    if v:
        import ipaddress
        try:
            ip = ipaddress.ip_address(v)
        except ValueError:
            raise ValueError("IP сервера - один адрес IPv4 или IPv6, без подсети") from None
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError("IP сервера - адрес конкретной машины")
        v = str(ip)
    return v


def agent_ip_valid(v: str) -> bool:
    try:
        return bool(_agent_ip_ok(v))
    except ValueError:
        return False


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    group_name: str = Field(default="", max_length=64)
    # IP, с которого агент будет ходить в панель (опционально, для фаервола)
    agent_ip: str = Field(default="", max_length=64)
    _agent_ip = field_validator("agent_ip")(_agent_ip_ok)
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
    _agent_ip = field_validator("agent_ip")(_agent_ip_ok)
    enabled: bool | None = None
    cpu_alert_percent: int | None = Field(default=None, ge=0, le=100)
    mem_alert_percent: int | None = Field(default=None, ge=0, le=100)
    disk_alert_percent: int | None = Field(default=None, ge=0, le=100)
    disk_warn_percent: int | None = Field(default=None, ge=0, le=100)
    disk_crit_percent: int | None = Field(default=None, ge=0, le=100)
    # авто-очистка диска: включает только админ (проверка в update_server)
    disk_autofix: bool | None = None
    temp_alert_c: int | None = Field(default=None, ge=0, le=120)
    conntrack_alert_percent: int | None = Field(default=None, ge=0, le=100)
    # пороги сроков Kubernetes в днях: [7, 1]; пустой список - выключено
    kube_expiry_warn_days: list[int] | None = None
    db_conn_alert_percent: int | None = Field(default=None, ge=0, le=100)
    web_5xx_alert_percent: float | None = Field(default=None, ge=0, le=100)
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
    parent_id: int | None = None  # None - ни за кем не стоит
    # лимит трафика тарифа (app/traffic.py): ГБ, 0 - без лимита; что считает хостер; день начала
    traffic_limit_gb: int | None = Field(default=None, ge=0, le=10_000_000)
    traffic_count: str | None = Field(default=None, pattern=r"^(out|both)$")
    traffic_cycle_day: int | None = Field(default=None, ge=1, le=28)

    _kube_days = field_validator("kube_expiry_warn_days")(_clean_warn_days)


class RelayUpdate(BaseModel):
    """Галочка "Ретранслятор для агентов" в настройках сервера (app/relay.py)."""
    enabled: bool
    port: int | None = Field(default=None, ge=0, le=65535)  # None - не менять, 0 - по умолчанию

    @field_validator("port")
    @classmethod
    def _port(cls, v: int | None) -> int | None:
        if v is not None and 0 < v < 1024:
            raise ValueError("порт ретранслятора - от 1024 до 65535")
        return v


class InstallViaIn(BaseModel):
    """Как агент этого сервера пойдет в панель: окно команды установки."""
    mode: str = Field(pattern=r"^(direct|relay|location|custom)$")
    relay_id: int | None = None
    location_id: int | None = None
    ip: str = Field(default="", max_length=64)  # внешний IP сервера: страна, проверки, фаервол ретранслятора


class InstallViaOut(BaseModel):
    proxy: str = ""  # для команды установки; пусто - напрямую или свой прокси (его знает только браузер)
    ip: str = ""


class InstallStatusOut(BaseModel):
    """Шаг 3 окна установки: что панель знает о новой ноде, пока агент не прислал отчет."""
    ip: str = ""  # IP сервера, который открывается в фаерволе панели
    # '' нечего сказать | open | opening | no_ip | stale (app/install_watch.firewall_state)
    firewall: str = ""
    fw_age: int | None = None  # секунд с последнего прогона скрипта открытия адресов
    relay: str = ""  # '' | opening | open: helper ретранслятора еще не пустил ноду / пустил
    script_at: datetime | None = None  # нода скачала установщик
    agent_at: datetime | None = None  # нода скачала бинарь агента
    connected: bool = False  # первый отчет пришел


class BackupAudit(BaseModel):
    """Находка аудита покрытия бэкапа: что на ноде рискует не восстановиться."""

    kind: str  # mount / bind / db
    subject: str  # путь или имя контейнера
    detail: str  # человекочитаемое пояснение
    # Одна короткая строка для карточки "Покрытие": без "где" (оно в чипе экземпляра) и без
    # общих слов, которые карточка пишет один раз в заголовке колонки. detail - полностью,
    # для подсказки и главной
    note: str = ""
    gap: bool  # True = данных нет в бэкапе; False = есть, но восстановимость под вопросом
    # для kind=db: код движка (pg/mysql/ch) — пусто, если движок дампить не умеем.
    # can_dump=True → панель снимет дамп сама (docker, локально, из пода через helper);
    # False → нельзя на этой ноде, причина — в detail.
    dump_engine: str = ""
    can_dump: bool = False
    # непусто → включение дампа стоит простоя (Neo4j Community умеет дамп только с
    # остановленной базы). Показываем в UI до включения, а не постфактум в графиках.
    downtime: str = ""
    container: str = ""
    pods: list[str] = []  # ns/name подов этого экземпляра — показать, где живёт база
    # ключ для точечного приглушения ("db:RabbitMQ") и признак «приглушена». Приглушённая
    # находка НЕ исчезает, а уезжает в свёрнутый список: иначе через месяц не вспомнить,
    # что спрятал её сам, и легко решить, что панель перестала проверять.
    key: str = ""
    muted: bool = False
    # конкретный экземпляр движка: имя контейнера или k8s.<ns>.<sts|deploy|ds>.<имя>.
    # Пусто = нативная установка. Входит в ключ: две postgres на ноде глушатся и дампятся
    # независимо.
    instance: str = ""
    # Примерно сколько придется бэкапить (dbstat-setup 0.15): у PostgreSQL и MySQL - базы без
    # индексов (в дамп они не идут), у остальных - каталог данных. 0 - не посчитано, строка молчит.
    size_bytes: int = 0
    size_db: int = 0  # база целиком, когда дамп заметно меньше нее
    size_top: str = ""  # крупнейшая база экземпляра, если баз несколько
    size_top_bytes: int = 0
    size_small: bool = False  # бэкапится малое при любом объеме данных (RabbitMQ: definitions)


class CustomBackupDb(BaseModel):
    """База внутри своего бэкапа — из его метрик (успех и время по каждой)."""

    name: str
    ok: int = -1  # 1 успех, 0 ошибка, -1 не сообщается
    ts: int = 0
    size_bytes: int = 0


class CustomBackupOut(BaseModel):
    """Свой бэкап ноды (настроен без панели) и его состояние.

    Находит helper на ноде; панель только следит. Статус: ok — отработал вовремя,
    failed — последний прогон упал, stale — давно не было успешного (или cron давно не
    запускал задание), disabled — таймер выключен, а бэкапа нет дольше обычного, off — таймер
    выключен, но задание еще ни разу не запускалось или прошлый прогон свежий (не алерт),
    running — идёт сейчас, ran — cron его запускает, но итога не видно, unknown — по заданию
    не видно даже запусков, ignored — отмечено «не отслеживать»."""

    id: str
    kind: str  # cron | systemd | metrics
    name: str
    desc: str = ""
    schedule: str = ""
    engines: list[str] = []  # какие СУБД бэкапит (по именам, командам, метрикам)
    files: bool = False  # файловый бэкап ноды (restic/borg/rsync…), а не дамп базы
    containers: list[str] = []  # на какие контейнеры ссылается
    status: str
    problem: str = ""  # что не так — для failed/stale/disabled
    ok_ts: int = 0  # последний успех (или самый свежий файл бэкапа, если статуса нет)
    run_ts: int = 0  # последний прогон вообще
    next_ts: int = 0
    size_bytes: int = 0
    duration_sec: int = 0
    dir: str = ""
    log: str = ""
    script: str = ""
    unit: str = ""
    result: str = ""  # systemd Result последнего прогона
    metrics: str = ""  # файл метрик, из которого взят статус
    # откуда время запуска: journal — журнал cron (старт), log — время записи лога, state —
    # отметка скрипта
    run_src: str = ""
    state: str = ""  # файл-отметка, в который скрипт пишет итог (backup.json)
    # чем подтверждён итог: state — отметка скрипта, restic — снимок в кэше restic,
    # files — свежий файл в каталоге бэкапа; у метрик и таймеров пусто
    evidence: str = ""
    fail: str = ""  # почему провал: state, state_old (отметка не обновилась), restic
    dbs: list[CustomBackupDb] = []
    stale_after: int = 0  # через сколько секунд молчание считается проблемой
    ignored: bool = False
    scan_stale: bool = False  # helper давно не обновлял сведения — статус неизвестен


class CustomBackupIgnoreIn(BaseModel):
    """Не отслеживать / снова отслеживать найденный на ноде бэкап."""

    id: str = Field(min_length=1, max_length=120)
    ignored: bool


class ExpirySnoozeIn(BaseModel):
    """Срок сайта: "напомнить за 7/1 дней" (mode 7/1), "не следить" (0) или вернуть (None)."""

    kind: Literal["ssl", "domain"]
    mode: Literal[7, 1, 0] | None = None


class KubeExpiryIgnoreIn(BaseModel):
    """Срок кластера "не нужен, не следить" - или вернуть его под присмотр."""

    kind: str = Field(min_length=1, max_length=40, pattern=r"^[a-z-]+$")
    where: str = Field(min_length=1, max_length=512)
    ignored: bool


class KubeNodeIgnoreIn(BaseModel):
    """Нода кластера, на которой агент не нужен, - или вернуть ее в подсказку."""

    node: str = Field(
        min_length=1, max_length=253, pattern=r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$"
    )
    ignored: bool


class BackupAuditMuteIn(BaseModel):
    """Приглушить/вернуть одну находку аудита покрытия (ключ вида "db:RabbitMQ")."""

    key: str = Field(min_length=1, max_length=200)
    muted: bool


class BackupToHttpsIn(BaseModel):
    """Перевести клиентов сервера бэкапов с http на https, по очереди."""

    client_ids: list[int] = Field(min_length=1, max_length=200)


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


class KubeNodeRef(BaseModel):
    """Нода кластера без агента панели: имя, адрес из Kubernetes и публичный ли он."""

    name: str
    ip: str = ""
    public: bool = False  # можно подставить в "IP сервера" при добавлении


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

    @field_validator("hostname", mode="before")
    @classmethod
    def _hostname_safe(cls, v):
        # имя, записанное до проверки на приеме (AgentReportIn), в команды тоже не попадает
        return safe_hostname(v) if isinstance(v, str) else ""
    local_ip: str
    external_ip: str
    agent_ip: str = ""
    # ISO-код страны по IP (офлайн-таблица, см. app/geoip.py); '' = не определилась
    country: str = ""
    # контейнеры, по которым алерт уже ушёл: {имя: "down"|"loop"} — панель подсвечивает
    # ровно их, а не всё подряд с ненулевым RestartCount
    docker_alerts: dict[str, str] = Field(default_factory=dict)
    # внешние прокси (caddy-docker-proxy, traefik) с docker-сокетом: [{"name", "image", "kind"}]
    docker_exposed: list[dict] = Field(default_factory=list)
    # контейнеры с ключами от хоста сверх положенного (docker_risk.view):
    # [{"name", "image", "flags", "caps", "text", "muted", "fix"?}], fix - шаги, общие у контейнеров проекта
    docker_risk: list[dict] = Field(default_factory=list)
    # внешние прокси со сборкой образа старше года (docker_exposure.outdated):
    # [{"name", "image", "kind", "version", "built", "age_days"}]
    proxy_outdated: list[dict] = Field(default_factory=list)
    # нода умеет разобрать раздел по запросу (агент 2.24 + diskusage-setup 0.6): кнопка "Разобрать"
    disk_analyze: bool = False
    # только что поставлена (первые 20 минут после первого отчета): установщик еще ставит helper'ы
    # и прокси Docker, "Требует действий" их не требует
    fresh: bool = False
    last_report: dict | None
    last_seen: datetime | None
    snooze_until: datetime | None = None
    alert_snoozes: dict[str, datetime] | None = None  # {тип: до} — точечный снуз
    # заметка о сервере: текст, кто и когда писал
    note: str = ""
    note_by: str = ""
    note_at: datetime | None = None
    # известные проблемы по видам алертов (app/known.py); worse - стало хуже отмеченного
    known_issues: dict[str, dict] | None = None
    # подключение нового сервера (app/onboard.py): идет - {until, tg, main, chats} (куда пойдут
    # алерты, имена чатов без id), закрыто - None
    onboard: dict | None = None
    # недоступен: ответил ли сервер панели в момент алерта (app/liveness.py) -
    # {state: alive|refused|silent, where, tried, ts}
    offline_probe: dict | None = None
    # агент предупредил о штатной остановке: {kind, at, wait} (app/going_down.py)
    going_down: dict | None = None
    # прогноз износа SSD по дискам из отчета (app/disk_wear.py)
    wear_forecast: list[dict] = Field(default_factory=list)
    onboard_done_at: datetime | None = None
    onboard_by: str = ""
    first_report_at: datetime | None = None
    # за каким сервером стоит (шлюз, VPN): пока тот молчит, "недоступен" про этот не шлем
    parent_id: int | None = None
    # ретранслятор для агентов на этом сервере (app/relay.py, без логина и пароля) и через
    # какой ретранслятор ходит агент этого сервера
    relay_enabled: bool = False
    relay_port: int = 0
    relay: dict | None = None
    relay_via_id: int | None = None
    created_at: datetime | None = None
    online: bool = False  # вычисляется (last_seen в пределах offline_after)
    cpu_alert_percent: int
    mem_alert_percent: int
    disk_alert_percent: int
    disk_warn_percent: int
    disk_crit_percent: int
    disk_autofix: bool = False  # авто-очистка безопасного при пороге предупреждения
    disk_forecast: dict | None = None  # когда заполнятся диски и inode (планировщик)
    temp_alert_c: int
    conntrack_alert_percent: int
    kube_expiry_warn_days: list[int] | None = None
    db_conn_alert_percent: int
    web_5xx_alert_percent: float = 0.05
    queue_alert_depth: int = 0
    # None, а не dict: в БД колонка nullable, и пустое значение приходит как NULL —
    # обязательный dict валил ServerOut пятисоткой на всём списке серверов
    queue_alert_over: dict | None = None
    disk_temp_alert_c: int
    alert_mutes: list[str] | None = None
    backup_repo_mutes: list[str] | None = None  # заглушённые репо бэкап-сервера (по имени)
    # архив бэкап-сервера: репозиторий или "репозиторий|хост" -> {note, ts, by}
    backup_repo_archive: dict | None = None
    # что открыто в интернет: последняя внешняя проверка портов (app/exposure.py)
    exposure: dict | None = None
    # что сделать с ОС ноды (os_support.advice): конец поддержки, перезагрузка, обновления
    os_advice: list[dict] | None = None
    # куда идет бэкап (backup_target.view): {kind, proto, host, port, repo, insecure, muted}
    backup_target: dict | None = None
    # бэкап по http: {bs_id, blockers} - куда переводить на https и что мешает (только в списке)
    backup_https: dict | None = None
    # версии без поддержки (software.advice): PHP, СУБД, прокси, у которых кончилась поддержка
    sw_advice: list[dict] | None = None
    # изменения на сервере (changes.view): лента и отметка "это мы"
    changes: dict | None = None
    # итог недельной проверки восстановления бэкапа (backup_verify.view)
    backup_verify: dict | None = None
    # перезапусков за сутки: {"docker:<имя>" | "pod:<ns>/<имя>": n} (restarts.view)
    restarts_day: dict[str, int] | None = None
    # раздел "Перезапуски" кластера (restarts.kube_view): поды с перезапусками за сутки, причина
    # прошлого завершения, пачки
    kube_restarts: dict | None = None
    # что течет памятью: [{key, kind, name, rate, eta_days, series, level, text}] (mem_trend.problems)
    mem_leaks: list[dict] | None = None
    # у своего лимита памяти (mem_limit.view): {"bad": тормозят сейчас, "near": у лимита без давления,
    # "past": эпизоды за сутки}; None - агент старше 2.51
    mem_limits: dict | None = None
    # OOM (app/oom.py): эпизодов за 7 дней, последний, юниты, убитые OOM ({юнит: {ep, line, quiet}})
    oom: dict | None = None
    # SSH (helper changes-setup 0.3): настройки входа, перебор, открыт ли порт (ssh_audit.view)
    ssh: dict | None = None
    # что сделать с SSH: вход по паролю на открытом порту (ssh_audit.advice) - в "Требует действий"
    ssh_advice: list[dict] | None = None
    # не переживет перезагрузку: {"docker": [{name, image, policy, muted}], "units": [{unit, started,
    # muted}]} (reboot_survive.view) и незаглушенные имена для "Требует действий" (reboot_survive.advice)
    noboot: dict | None = None
    noboot_advice: dict | None = None
    # учетки с shell: {"accounts": [{name, uid, pw, last, keys, weak, nopasswd, access, stale, muted}],
    # "seen_since"} (access_audit.view) и имена для "Требует действий": {"stale", "weak"}
    access: dict | None = None
    access_advice: dict | None = None
    # здоровье баз по движкам: роль, отставание, транзакции, проблемы (db_health.view)
    db_health: list[dict] | None = None
    # GPU NVIDIA: карты, процессы, Xid, драйвер (gpu.view) и плашка списка (gpu.summary)
    gpu: dict | None = None
    gpu_sum: dict | None = None
    # LLM-сервисы с обычными задержками (gpu.llm_view)
    llm: list[dict] | None = None
    # очереди, которые не разбираются или стоят: {ключ очереди: {"age", "d0", "muted", "stall", "wait",
    # "why"}} (queue_watch.view)
    queue_watch: dict | None = None
    # очереди от helper'а queues-setup (Redis, таблицы баз): брокеры как у RabbitMQ, с подписью "where" и
    # упавшими задачами "failed" (queue_watch.helper_view)
    queue_brokers: list[dict] | None = None
    # экземпляры Redis, пароль которых helper не нашел
    queue_skipped: list[str] | None = None
    # базы, куда helper не вошел, и слишком большие таблицы очередей: [{why, where, rows}] (queue_watch.db_skipped)
    queue_db_skipped: list[dict] | None = None
    # заглушенные очереди: {ключ: {"perm": true} | {"until": iso}} (queue_watch.mute_view)
    queue_mutes: dict | None = None
    # заглушенный всплеск упавших задач брокеров: {источник брокера: {"perm": true} | {"until": iso}}
    queue_fail_mutes: dict | None = None
    # воркеры под supervisor и pm2: демоны с программами, беда и мьют у каждой (workers.view)
    workers: list[dict] | None = None
    # демоны, которых helper workers-setup не смог спросить: [{where, why}]
    workers_skipped: list[dict] | None = None
    # кроны хоста и контейнеров (helper cron-setup): задачи, копии, беда и мьют (cron_watch.view)
    cron: dict | None = None
    # ошибки в логах контейнеров и подов (helper logerr-setup): счет, обычный час, всплеск (log_errors.view)
    log_errors: dict | None = None
    # пулы PHP-FPM (helper fpm-setup): конфиг, очередь, счет за час и сутки, беда (fpm_pools.view)
    fpm: dict | None = None
    # GitLab (helper gitlab-setup): версия и путь обновления, бэкап, миграции, автообновление (gitlab.view)
    gitlab: dict | None = None
    # проверка конфигов веб-серверов: [{server, where, ok, err}] (web_config.view)
    web_config: list[dict] | None = None
    # сертификаты nginx со сроком в днях: [{where, file, label, days}] (web_certs.view)
    web_certs: list[dict] | None = None
    # файлы сайтов: веб-корни, новые php и правки вне выкладки (webfiles.view)
    webfiles: dict | None = None
    backup_audit_mutes: list[str] | None = None  # приглушённые находки покрытия (по ключу)
    backup_not_required: bool = False  # у сервера бэкап не требуется (алерт снят)
    db_dumps_ok: bool = False  # дампы СУБД настроены отдельно (пункт снят с главной)
    backup_deadline_hour: int = 8  # окно бэкапа: до какого часа должен закончиться
    backup_anytime: bool = False  # бэкап в любое время — не уведомлять о выходе за окно
    traffic_limit_gb: int = 0  # лимит трафика тарифа, ГБ (0 - нет)
    traffic_count: str = "out"
    traffic_cycle_day: int = 1
    offline_after_seconds: int
    alert_sustain_seconds: int = 900
    # самодиагностика: чего агенту не хватает в systemd-юните + команда-фикс (или пусто)
    agent_advice: list[str] = []
    agent_fix_command: str | None = None
    # идёт докачка обновления агента (медленный канал) - не отказ, чинить нечего
    agent_update_note: str | None = None
    # агент отверг обновление (подпись, место, сеть): причина его словами, иначе None.
    # Главная показывает ее у ноды, которая не обновилась после "Обновить все"
    agent_update_error: str | None = None
    # устаревшие setup-скрипты (helper'ы) на ноде → переустановить на детали сервера
    helper_advice: list[HelperAdvice] = []
    # Прогон kervax_helpers.yml ноде что-то поменяет: устарел helper или не включен
    # watchdog. Считается здесь, а не на фронте: тот же признак отдает ansible
    # (/api/ansible/servers), и расходиться им нельзя.
    helper_rollout: bool = False
    # аудит покрытия бэкапа: что рискует не восстановиться (показ, БЕЗ алертов)
    backup_audit: list[BackupAudit] = []
    # свои бэкапы ноды, найденные helper'ом (cron, таймеры, метрики) — со статусом
    custom_backups: list[CustomBackupOut] = []
    # Что сломано по новым проверкам (диски, прогноз, inode, юниты): kind, level 1-3, text,
    # sec - куда вести в карточке, mute - ключ приглушения. Считает collector.server_problems.
    problems: list[dict] = []
    custom_backup_ignored: list[str] | None = None
    kube_node_ignored: list[str] | None = None  # ноды кластера, где агент не нужен (по имени)
    # сроки кластера с отметкой "не следить": {"<kind>|<where>": {by, at}}, только действующие
    kube_expiry_ignored: dict[str, dict] | None = None
    # TLS-секреты, на которые ничего не ссылается (helper kubeexpiry-setup 0.4): "<kind>|<where>"
    kube_expiry_unused: list[str] | None = None
    # с какого момента видна проблема каждого вида (ISO) - для "Что сломано": "2 дня"
    alert_since: dict[str, str] = Field(default_factory=dict)
    # с какого момента лежит каждый упавший контейнер: {имя: ISO}
    docker_since: dict[str, str] = Field(default_factory=dict)
    # последний прогон бэкапа добавил в разы больше обычного: {level, text, st, usual, ts}
    backup_growth: dict | None = None
    # настроен ли бэкап (app/backup_state.py): {state: ok|partial|none, reason, since, traces} -
    # одно правило для главной, раздела "Бэкапы" и формы настройки
    backup_state: dict | None = None
    # начало uid пода -> ns/имя: агент воркера без доступа к kube-api знает поды по uid
    pod_names: dict[str, str] = Field(default_factory=dict)
    # ноды кластера без агента панели; считает kube_coverage по ВСЕМ серверам панели
    kube_unmonitored: list[KubeNodeRef] = []
    # ключи заглушки до нового пунктов "Требует действий" (app/home_mute.py): {группа: home:<группа>@<отпечаток>},
    # только у пунктов, которые сейчас есть; только в списке серверов
    home_mutes: dict[str, str] = Field(default_factory=dict)
    # CronJob кластера с проблемой (упал, давно нет успеха, у дампа - приостановлен): {ns/имя: причина}
    kube_cron_issues: dict[str, str] = Field(default_factory=dict)
    # репозитории бэкап-сервера со свежими бэкапами, чьих клиентов нет в панели (по ВСЕМ серверам)
    bsrv_unmonitored: list[str] = []
    # что сейчас сказал бы алерт "ротация встала" (для "Что сломано" на главной)
    bsrv_rotation: list[str] = []
    # вкладка "Чистка" (app/bsrv_cleanup.py): старый общий скрипт, его блоки на удаленные репозитории,
    # где старье не вычищается и почему, где не работает чистка; у каждого пункта шаги fix
    bsrv_cleanup: dict | None = None
    # restic самого сервера бэкапов старше той версии, до которой его обновляет helper
    bsrv_restic_old: str = ""
    # репозитории, которые сейчас попали бы в алерт backup_repo: имя -> причина (по ВСЕМ серверам:
    # с ритмом бэкапов и с тем, есть ли клиент в панели)
    bsrv_repo_reasons: dict[str, str] = Field(default_factory=dict)
    # обычный интервал бэкапов репозитория, секунды (по последним снапшотам, helper 0.28)
    bsrv_cadence: dict[str, int] = Field(default_factory=dict)
    # репозитории, у которых не работает чистка: имя -> причина (не открывает, падает, не идет)
    bsrv_prune: dict[str, str] = Field(default_factory=dict)


class BackupRepoMuteIn(BaseModel):
    """Заглушить/включить конкретный репозиторий бэкап-сервера (по имени)."""

    repo: str = Field(min_length=1, max_length=253)
    muted: bool


class BackupRepoArchiveIn(BaseModel):
    """Убрать в архив или вернуть репозиторий бэкап-сервера, а с host - одну его старую группу."""

    repo: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9._-]+$")
    host: str = Field(default="", max_length=253, pattern=r"^[A-Za-z0-9._-]*$")
    note: str = Field(default="", max_length=200)
    archived: bool


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
    web_rpm: float | None = None  # запросов в минуту по access-логам веб-сервера
    web_5xx: float | None = None  # из них ответов 5xx в минуту
    web_top: list[dict] | None = None  # [{"k","r","e"}] самые нагруженные логи
    web_codes: list[dict] | None = None  # [{"c","n"}] ответы 5xx по кодам
    gpu: list[dict] | None = None  # [{"g","u","m","t","p"}] по картам: загрузка, память, температура, питание
    llm: list[dict] | None = None  # [{"k","run","wait","kv","gen","req","ttft","tpot","e2e"}] по LLM-сервисам


class WebErrorPath(BaseModel):
    p: str
    n: int


class WebErrorOut(BaseModel):
    """Ошибки 5xx одного лога за окно: страница, на которую ведёт алерт."""

    key: str = ""  # ключ лога для глушения: web_5xx:<key>
    log: str
    label: str
    errors: int          # всего ответов 5xx за окно
    minutes: int         # сколько минут были ошибки
    peak: int            # максимум ошибок в минуту
    first_ts: datetime
    last_ts: datetime
    codes: dict[str, int] = {}
    paths: list[WebErrorPath] = []
    # по доменам, если лог общий и в его формате есть $host (helper 0.21): [{"h": "site", "n": 41}]
    hosts: list[dict] = []
    lines: list[str] = []  # последние строки с 5xx (helper 0.19), старые сверху
    lines_n: int = 0       # сколько строк сохранено за окно - все отдает /web-errors/lines
    # почему 502/504 (helper 0.26): [{"u": адрес, "k": вид, "n": за окно, "c": контейнер}]
    ups: list[dict] = []
    # лога больше нет в отчете: сайты разнесли по своим логам, контейнер удалили. Ошибки за
    # окно остаются видны, но это история, а не то, что падает сейчас
    gone: bool = False


class OomEventOut(BaseModel):
    """Строка журнала OOM-киллов: когда, кого убило, сколько за интервал."""

    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    victim: str
    count: int


# --- агент → панель ---


class AgentGoingDownIn(BaseModel):
    """Агент уходит штатно: reboot, poweroff, shutdown или stop (app/going_down.py)."""
    kind: str = Field(default="shutdown", max_length=16)


# Имя хоста от ноды уходит в команды ansible, которые человек копирует себе в терминал
# (`-l <hostname>`). Взломанная нода могла прислать `x;curl ...|sh;#` и получить выполнение на
# управляющей машине. Пропускаем только то, что бывает в именах хостов
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


def safe_hostname(value: str) -> str:
    """Имя хоста, годное для подстановки в команду, иначе пустая строка."""
    value = (value or "").strip()
    return value if _HOSTNAME_RE.fullmatch(value) else ""


class AgentReportIn(BaseModel):
    hostname: str = Field(default="", max_length=255)

    @field_validator("hostname", mode="before")
    @classmethod
    def _hostname_ok(cls, v):
        # отчет не роняем: старые агенты и странные имена продолжают слать метрики, имя просто не берем
        return safe_hostname(v) if isinstance(v, str) else ""

    os: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=32)
    cpu_model: str = Field(default="", max_length=128)
    is_vm: bool = False
    virt: str = Field(default="", max_length=64)
    local_ip: str = Field(default="", max_length=64)
    # агент 2.44+ за прокси: хост:порт прокси (без логина), адрес соединения - это прокси
    proxy: str = Field(default="", max_length=255)
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
    # сроки PKI кластера, kubeconfig'ов, TLS-секретов и кредов Flux
    # (хелпер kubeexpiry-setup): [{kind, where, expires, note, fp?, ingress?}]; fp - отпечаток
    # сертификата, ingress - TLS-секрет указан хоть в одном Ingress (с helper'а 0.3 и агента 2.49)
    kube_expiry: list[dict] = []
    # результаты локальных проверок сайтов, снятые агентом изнутри сервера:
    # [{id, code, latency_ms, error, kw_up_found, kw_down_found}]
    site_probes: list[dict] = []
    # состояния Ready ресурсов Flux: [{kind, where, ready, reason, message}]
    flux: list[dict] | None = None
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
    # агент 2.42: сам счетчик ядра (/proc/vmstat oom_kill), рост считает панель от прошлого
    # отчета - убийства не теряются, когда агент перезапустился посреди OOM
    oom_total: int | None = None
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
    # кто ест CPU группами (агент 2.21): [{comm, kind, owner, n, cpu, spin, spin_age}]. Агент слал
    # их с 06.10.2026, а здесь поля не было - pydantic выбрасывал, и cpu_spin молчал до 1.4.250.
    # None - агент старше 2.21 (или групп нет вовсе): молчание не значит, что никто не крутит
    cpu_groups: list[dict] | None = Field(default=None, max_length=200)
    # кто пишет на диск за последний час (агент 2.33): [{kind, name, w, r, s}]
    disk_writers: list[dict] = Field(default_factory=list, max_length=50)
    disk_writers_v1: bool = False
    # у своего лимита памяти (агент 2.51): [{kind, name, u, lim, cur, anon, file, some, full, f10, mf, rf}].
    # None - агент старый; [] - никого у лимита (app/mem_limit.py)
    mem_limits: list[dict] | None = Field(default=None, max_length=50)
    # кто качает трафик за последний час (агент 2.34): [{kind, name, rx, tx, s}]
    net_users: list[dict] = Field(default_factory=list, max_length=50)
    # видеокарты NVIDIA на шине PCI (агент 2.36): [{bus, dev, drv}]
    gpu_pci: list[dict] = Field(default_factory=list, max_length=32)
    # LLM-сервисы (агент 2.36): [{engine, kind, name, model, port, ok, running, waiting, kv, ttft...}]
    llm: list[dict] = Field(default_factory=list, max_length=32)
    conntrack_count: float = 0.0
    conntrack_max: float = 0.0
    sock_used: float = 0.0
    sock_tcp: float = 0.0
    sock_tcp_tw: float = 0.0
    sock_udp: float = 0.0
    checks: list[dict] = []  # [{"key","type","status","message"}]
    caps: dict[str, bool] = {}  # самодиагностика прав из юнита ({"kmsg": true/false})
    # Причина, по которой агент отказался ставить обновление (подпись/хеш/сеть).
    # Без этого поля отказ виден только в journalctl ноды, а в панели — лишь
    # расхождение «цель X / версия Y» без объяснения.
    update_error: str = Field(default="", max_length=300)
    docker: dict | None = None  # снимок Docker: {present,access,version,compose,containers}
    kube: dict | None = None  # снимок Kubernetes: {present,access,flavor,version,nodes,pods,…}
    backup: dict | None = None  # статус restic-бэкапа: {present,success,last_backup_ts,skipped,…}
    backup_server: dict | None = None  # rest-server: {present,running,version,repos:[{name,valid,…}]}
    setup_versions: dict | None = None  # версии setup-скриптов на ноде: {backup-setup:1, kube-setup:1,…}
    clock: dict | None = None  # статус синхронизации времени: {synced,ntp,service}
    clock_unix: int = 0  # локальные часы ноды на момент отправки (для расчёта сдвига панелью)
    # DNS ноды (агент 2.25): {mode, servers:[{addr,ms,err}], miss_ms, miss_err, miss_ts, ts}
    dns: dict | None = None
    # агент 2.26: слушающие сокеты [{p, ip, port, u}], публичные адреса интерфейсов, давление
    # (/proc/pressure), процессы в D-state, дескрипторы, TCP (ретрансмиты, очередь accept,
    # дропы conntrack) и обновления ОС (перезагрузка, обновления, автообновления)
    listen: list[dict] = []
    ips: list[str] = []
    psi: dict | None = None
    dstate: dict | None = None
    fds: dict | None = None
    tcp: dict | None = None
    os_upd: dict | None = None
    # агент 2.28: отпечаток списка пакетов {mgr, n, h, log} и новые строки журнала установок
    pkgs: dict | None = None
    pkg_ev: list[dict] = Field(default_factory=list, max_length=600)
    # агент 2.31: связь с другими серверами [{id, sent, ok, rtt_ms, rtt_max, err}] (app/netlinks.py)
    links: list[dict] = Field(default_factory=list, max_length=64)
    # блоки root-хелперов как есть: {"custom-backups": {...}} (агент 2.8+, report.d)
    extras: dict | None = None


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
    # Сайты, которые агент проверяет САМ, изнутри сервера: панель до них не дотянется
    # (белый список IP). [{id,url,method,timeout_ms,expected_status,keyword_up,
    #  keyword_down,headers,auth_user,auth_pass,ignore_tls,interval}]
    site_probes: list[dict] = []
    # у панели другой отпечаток списка пакетов: агент шлет список кусками (app/packages.py)
    want_pkgs: bool = False
    # связь с другими серверами: куда пробовать открыть TCP-соединение [{id, host, port}]
    links: list[dict] = []


class AgentPackagesIn(BaseModel):
    """Кусок списка пакетов от агента."""

    h: str = Field(min_length=1, max_length=32)
    mgr: str = Field(default="", max_length=8)
    part: int = Field(ge=0, le=79)
    parts: int = Field(ge=1, le=80)
    items: list[list[str]] = Field(default_factory=list, max_length=600)


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


class AgentSiteProbesIn(BaseModel):
    """Агент → панель: ответы на ручную проверку сайта (быстрый путь агента 2.7+)."""

    site_probes: list[dict] = Field(default_factory=list, max_length=50)


class DockerResultIn(BaseModel):
    """Агент → панель: результат исполнения команды."""

    id: int
    ok: bool
    output: str = Field(default="", max_length=22_000_000)  # логи за день бывают крупными


# k8s DNS-1123: строго валидируем имена/namespace (агент валидирует повторно)
_K8S_NAME = r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$"


class KubeCommandIn(BaseModel):
    """Панель → очередь: управляющее действие над ресурсом кластера сервера.
    Только белый список действий: rollout_restart (deploy/sts/ds), delete_pod, logs и logs_prev
    (лог прошлого запуска, kubectl logs --previous; агент 2.43+)."""

    ns: str = Field(min_length=1, max_length=253, pattern=_K8S_NAME)
    kind: str = Field(default="pod", pattern="^(deployment|statefulset|daemonset|pod)$")
    name: str = Field(min_length=1, max_length=253, pattern=_K8S_NAME)
    action: str = Field(pattern="^(rollout_restart|delete_pod|logs|logs_prev)$")
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
_BACKUP_PATH = r"^/[A-Za-z0-9._/+-]+$"
# Каталог дампов helper создает с правами 0700 (и сам каталог, если он уже есть), а "Выключить
# дамп" удаляет каталог движка целиком. /var/lib со службами, /etc, /usr так ломали ноду от root:
# системные деревья нельзя совсем, верхние каталоги (/home, /var, /opt...) - только подкаталогом
_DUMP_DENY_TREES = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
                    "/proc", "/sys", "/dev", "/run", "/tmp", "/var/lib", "/var/run", "/var/tmp",
                    "/var/log", "/var/spool", "/var/cache", "/snap")
_DUMP_DENY_EXACT = ("/home", "/root", "/var", "/srv", "/opt", "/mnt", "/media", "/data")
# каталог по умолчанию (DUMP_DIR_BASE в agent/backup-setup.sh): его и подкаталоги задает editor,
# любой другой - только админ (api/servers.py backup_command)
DUMP_DIR_DEFAULT = "/backup"


def dump_dir_safe(v: str) -> bool:
    p = "/" + "/".join(x for x in v.split("/") if x and x != ".")
    if p in _DUMP_DENY_EXACT:
        return False
    return not any(p == t or p.startswith(t + "/") for t in _DUMP_DENY_TREES)


def dump_dir_default(v: str) -> bool:
    p = "/" + "/".join(x for x in (v or "").split("/") if x and x != ".")
    return not v or p == DUMP_DIR_DEFAULT or p.startswith(DUMP_DIR_DEFAULT + "/")  # '+' — для /lost+found (стандартный exclude ext-ФС)


_UNIT_NAME = r"^$|^[A-Za-z0-9@._:\\][A-Za-z0-9@._:\\-]*\.(service|socket|timer|mount|automount|swap|path)$"
# unit_journal читает еще весь журнал (_all) и ядро (_kernel)
_UNIT_OR_ALL = _UNIT_NAME + r"|^_(all|kernel)$"


class BackupCommandIn(BaseModel):
    """Панель → очередь: управление restic-бэкапом ноды (через узкий helper).
    Только белый список: set_paths (include/exclude), set_schedule, run_now."""

    action: str = Field(pattern="^(set_paths|set_schedule|run_now|dump_setup|dump_remove|restic_update|update_image|timesync|disk_fix|unit_fix"
                        "|unit_journal|deprovision)$")
    # include/exclude - для путей бэкапа; preview/run - для disk_fix; restart/reset - для unit_fix
    mode: str = Field(default="exclude", pattern="^(include|exclude|preview|run|restart|reset)$")
    paths: list[str] = Field(default_factory=list, max_length=200)
    schedule: str = Field(default="", pattern=r"^$|^([01][0-9]|2[0-3]):[0-5][0-9]$")
    # dump_setup: локальные дампы СУБД перед файловым бэкапом
    # Список ДОЛЖЕН совпадать с _DUMP_ENGINE в api/servers.py и с разбором в
    # agent/backup-setup.sh: забытый движок молча отваливался бы 422-й на кнопке
    # «включить дампы» (так уже вышло с grafana).
    engine: str = Field(default="", pattern="^$|^(pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j)$")
    # имя контейнера или цель в kubernetes: k8s.<namespace>.<sts|deploy|ds>.<имя>
    container: str = Field(default="", max_length=160, pattern=r"^$|^[A-Za-z0-9._-]+$")
    # настройки дампа: каталог, сколько последних хранить, минимум свободного места (%).
    # Пусто/0 у dump_dir → helper берёт дефолт /backup. dump_keep 1..30, dump_minfree 0..50.
    dump_dir: str = Field(default="", max_length=200)
    dump_keep: int = Field(default=0, ge=0, le=30)
    dump_minfree: int = Field(default=10, ge=0, le=50)
    # disk_fix: кнопка "Освободить" из разбора места. Тот же каталог знают агент и helper
    # diskusage-setup; разрешено ли действие на ноде, решает helper по /etc/kervax/fix.conf.
    fix: str = Field(default="", pattern="^$|^(journal|rotated-logs|apt-cache|dnf-cache|coredumps"
                     "|crash-reports|docker-dangling|docker-build-cache|container-log|analyze)$")
    # disk_fix analyze: разобрать раздел, который helper сам не разбирает (ниже 75%)
    mount: str = Field(default="", max_length=256, pattern=r"^$|^/[A-Za-z0-9/._-]*$")
    # unit_fix: упавший юнит systemd (helper units-setup трогает его, только пока тот упал).
    # Обратный слэш - так systemd экранирует имена mount-юнитов (mnt-my\x20disk.mount).
    unit: str = Field(default="", max_length=200, pattern=_UNIT_OR_ALL)
    # unit_journal: журнал юнита (или _all / _kernel) за последние since_min минут, не больше
    # lines строк, приоритет не ниже prio. Те же пределы зажимают агент и helper.
    since_min: int = Field(default=60, ge=1, le=10080)
    lines: int = Field(default=300, ge=10, le=1000)
    prio: str = Field(default="info", pattern="^(err|warning|info)$")

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
        if v and (not re.match(_BACKUP_PATH, v) or ".." in v or v == "/" or not dump_dir_safe(v)):
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
    # на ноде уже есть бэкап: переписать его настройку на клиенте (репозиторий и история на
    # бэкап-сервере остаются, если он тот же)
    reinstall: bool = False

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


class BackupToHttpsJobOut(BackupSetupJobOut):
    """Задача перевода одного клиента на https в пачке: чей это бэкап."""

    server_id: int


class AgentReleaseOut(BaseModel):
    """Доступная (подписанная) версия агента + текущие версии setup-скриптов (панель
    сверяет с установленными на нодах и флагует устаревшие helper'ы)."""

    version: str  # пусто, если релиз-манифест не найден
    setup_versions: dict = Field(default_factory=dict)  # {backup-setup:1, kube-setup:1, …}
    problem: str = ""  # раздаваемый бинарь разошёлся с подписанным манифестом


class AgentUpdateReq(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    server_ids: list[int] | None = None  # None = все включённые (иначе canary/подмножество)
    # запомнить запуск на панели: прогресс в "Обновлениях" переживает перезагрузку страницы
    track: bool = False


class AgentUpdateCancel(BaseModel):
    server_ids: list[int] | None = None  # None = снять со всех
    # "стоп" в "Обновлениях": эти ноды уходят и из хода раскатки
    untrack: bool = False


class HelperRolloutReq(BaseModel):
    """Команду ansible скопировали: ждем прогона на этих хостах."""

    hosts: list[str] = Field(max_length=5000)


class AgentRunOut(BaseModel):
    v: str
    ids: list[int]
    at: datetime


class HelperRunOut(BaseModel):
    hosts: list[str]
    at: datetime


class FleetRolloutOut(BaseModel):
    """Ход обновления парка (fleet_rollout.py): части нет - ничего не запускали или прошли сутки."""

    agent: AgentRunOut | None = None
    helpers: HelperRunOut | None = None


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


class NoteIn(BaseModel):
    """Заметка к серверу или монитору; пустая строка - убрать."""
    text: str = Field(default="", max_length=2000)


class KnownIn(BaseModel):
    """Отметить проблему известной: что про нее знают и на сколько дней (0 - пока не уйдет)."""
    text: str = Field(min_length=1, max_length=500)
    days: int = Field(default=0, ge=0, le=90)


class MaintIn(BaseModel):
    """Окно обслуживания. Проверки (конец позже начала, длина, дни недели) - в API: там они
    отвечают человеку строкой, а не списком ошибок pydantic."""
    title: str = Field(min_length=1, max_length=200)
    server_ids: list[int] = Field(default_factory=list, max_length=1000)
    check_ids: list[int] = Field(default_factory=list, max_length=1000)
    starts_at: datetime
    ends_at: datetime
    repeat: Literal["", "daily", "weekly"] = ""
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    verify: bool = True
    note: str = Field(default="", max_length=1000)


class MaintOut(BaseModel):
    id: int
    title: str
    server_ids: list[int]
    check_ids: list[int]
    starts_at: datetime
    ends_at: datetime
    repeat: str
    weekdays: list[int]
    verify: bool
    note: str
    created_by: str
    active: bool = False             # идет прямо сейчас
    cur_end: datetime | None = None  # когда кончится текущее вхождение
    next_start: datetime | None = None
    finished: bool = False           # разовое и уже прошло
