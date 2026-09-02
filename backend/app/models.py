"""SQLAlchemy-модели Kervax.

  - User      — учётка админа (пароль + 2FA + версия токена)
  - AuditLog  — журнал действий и событий входа
  - AppSetting — редактируемые из UI настройки (kv поверх env)
  - Check      — монитор сайта/сервиса (http / tcp_port / cert)
  - CheckSample — тайм-серия результатов проверок

Дальше (Этап 3): CheckIncident (инциденты/uptime); Этап 5: Server/NodeMetric (SSH).

Модуль импортируется alembic/env.py, чтобы наполнить Base.metadata.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    # Роль: admin — всё, включая учётки и настройки панели; editor — правки в
    # разрешённых разделах, но НЕ учётки/настройки; viewer — только просмотр.
    role: Mapped[str] = mapped_column(String(16), default="admin")
    # Разделы, доступные учётке (["sites","servers",…]). Пусто/NULL = все.
    # Ими же рисуется верхнее меню: чего нет в списке — того не видно и не открыть.
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Видимые группы. У серверов и у сайтов свои наборы имён и смешивать их нельзя:
    # группа «Shop» у мониторов и «Infra» у нод — разные сущности, и общий список
    # означал бы «выдай доступ к чему-то одноимённому». Пусто/NULL = все группы.
    # Ограничение сквозное: и списки, и открытие конкретного объекта по id.
    server_groups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    site_groups: Mapped[list | None] = mapped_column(JSON, nullable=True)
    totp_secret: Mapped[str] = mapped_column(String(64), default="")
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # последний использованный TOTP-счётчик (защита от повторного использования кода)
    totp_last_counter: Mapped[int] = mapped_column(BigInteger, default=0)
    # версия токена: смена пароля инкрементит её и инвалидирует старые JWT
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # Персональные алерты в Telegram. Чат — личный; токен пустой = шлём общим ботом
    # панели (сотруднику не нужен BotFather, достаточно нажать Start и привязаться).
    tg_chat_id: Mapped[str] = mapped_column(String(64), default="")
    tg_token: Mapped[str] = mapped_column(String(256), default="")
    tg_alerts: Mapped[bool] = mapped_column(Boolean, default=True)
    tg_link_code: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoginFailure(Base):
    """Неудачные попытки входа — счётчик защиты от подбора пароля.

    Живёт в базе, а не в памяти процесса: в scale-режиме uvicorn поднимает
    несколько воркеров, и у каждого был бы свой счётчик. Порог тогда умножается
    на число процессов (10 попыток превращались в 30 при трёх воркерах), причём
    молча — снаружи это выглядит как работающая защита.
    """

    __tablename__ = "login_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), index=True)  # обычно IP клиента
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AuditLog(Base):
    """Журнал действий: кто/когда/что сделал (вход, смена пароля, мониторы…)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """Настройки панели (key-value), редактируемые из UI. Значение — строка/JSON."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Check(Base):
    """Монитор сайта/сервиса. Тип определяет исполнителя (см. checks.py)."""

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(16))  # http | tcp_port | cert
    target: Mapped[str] = mapped_column(String(512))  # URL (http) или host (tcp/cert)
    port: Mapped[int] = mapped_column(Integer, default=0)  # tcp/cert; 0 = дефолт (443)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    group_name: Mapped[str] = mapped_column(String(64), default="")
    # ручной порядок в списке (перетаскивание); меньше = выше. Тайбрейк — id
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # расписание/пороги
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=10000)
    degraded_ms: Mapped[int] = mapped_column(Integer, default=2000)  # медленнее → degraded
    # повторные попытки при неуспехе: статус «плохой», только если провалились ВСЕ
    # (гасит одиночные сетевые блипы до открытия инцидента/алерта)
    retries: Mapped[int] = mapped_column(Integer, default=3)
    # алерт слать только после N «плохих» проверок подряд (гасит редкие флапы)
    alert_after_failures: Mapped[int] = mapped_column(Integer, default=3)
    # отдельный, обычно больший порог для деградации (медленно) — она шумнее
    degraded_after_failures: Mapped[int] = mapped_column(Integer, default=5)

    # http
    method: Mapped[str] = mapped_column(String(8), default="GET")
    expected_status: Mapped[str] = mapped_column(String(32), default="200-399")
    keyword_up: Mapped[str] = mapped_column(String(256), default="")   # тело ДОЛЖНО содержать
    keyword_down: Mapped[str] = mapped_column(String(256), default="")  # тело НЕ должно содержать
    # быстрый снуз: не слать алерты по этому монитору до этого момента (None = не
    # заглушён). Авто-истекает по времени, чистить не нужно.
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # HTTP-аутентификация (для сайтов за Basic-auth: 401 без логина/пароля).
    # auth_method: "" = нет, "basic" = HTTP Basic. Пароль хранится как есть —
    # панель под доступом; отдаётся в API для редактирования (как telegram_token).
    auth_method: Mapped[str] = mapped_column(String(16), default="")
    auth_user: Mapped[str] = mapped_column(String(128), default="")
    auth_pass: Mapped[str] = mapped_column(String(256), default="")
    # проверять КАЖДЫЙ IP домена (A-записи), а не только тот, что выбрал резолвер —
    # ловит мёртвый бэкенд за балансировщиком. По умолчанию выкл. (только http).
    check_all_ips: Mapped[bool] = mapped_column(Boolean, default=False)
    # кастомные HTTP-заголовки (JSON-текст, напр. {"x-application-token":"…"}) —
    # для сайтов, требующих токен/заголовок (иначе 401). Пусто = без доп. заголовков.
    http_headers: Mapped[str] = mapped_column(String(4096), default="")
    # не проверять TLS-сертификат основной проверкой (самоподписанный / hostname
    # mismatch / истёкший, но сайт нужно мониторить). По умолчанию выкл.
    ignore_tls: Mapped[bool] = mapped_column(Boolean, default=False)
    # Сайт закрыт снаружи (белый список IP), и панель до него не дотянется: проверять
    # будет агент НА САМОМ СЕРВЕРЕ. Панель к такому монитору не ходит вовсе, иначе он
    # вечно «недоступен».
    #
    # Воля пользователя — это ГАЛОЧКА probe_local. Какой именно сервер проверяет, он
    # не выбирает: панель и так знает, чьи веб-серверы обслуживают этот домен (агенты
    # присылают их в web_services), а держать этот выбор в голове человека — лишняя
    # работа и лишний повод ошибиться. probe_server_id — вычисленная привязка, её
    # планировщик пересчитывает: сайт переезжает с ноды на ноду, галочка остаётся.
    probe_local: Mapped[bool] = mapped_column(Boolean, default=False)
    probe_server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # доп. проверки для http-мониторов (по умолчанию включены) —
    # срок/валидность TLS-сертификата и срок регистрации домена (RDAP)
    check_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    check_domain: Mapped[bool] = mapped_column(Boolean, default=True)
    # пороги напоминаний (дней до истечения) — эскалация: SSL [14,7,1], домен [7,1]
    ssl_warn_days: Mapped[list[int]] = mapped_column(JSON, default=lambda: [14, 7, 1])
    domain_warn_days: Mapped[list[int]] = mapped_column(JSON, default=lambda: [7, 1])
    # проверять этот сайт ещё и через прокси-локации (см. Location) — по умолчанию вкл.
    check_locations: Mapped[bool] = mapped_column(Boolean, default=True)
    # какие именно локации использовать: None = все включённые (дефолт),
    # [] = ни одной, [id,…] = выбранное подмножество
    location_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True, default=None)
    # типы алертов, заглушённые ДЛЯ ЭТОГО монитора (переопределяют глобальные правила)
    alert_mutes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # дедуп алертов частичной доступности: набор id «недоступных» локаций на момент
    # последнего алерта (None = не в состоянии частичной недоступности)
    loc_alerted: Mapped[list[int] | None] = mapped_column(JSON, nullable=True, default=None)
    # С какого момента держится нынешняя частичная недоступность. Нужно, чтобы
    # отличить её от ФАЗЫ ВОССТАНОВЛЕНИЯ: после полного падения точки поднимаются
    # не одновременно, и «из СПб видно, из Алматы нет» — это сайт встаёт, а не
    # проблема с доступностью из региона. Сбрасывается при полном падении.
    loc_partial_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # О каком наборе недоступных точек УВЕДОМИЛИ. Отдельно от loc_alerted: тот
    # ведёт интерфейс и обновляется сразу, а этот — только после реально
    # отправленного алерта, и по нему решается, нужен ли отбой.
    loc_notified: Mapped[list[int] | None] = mapped_column(JSON, nullable=True, default=None)
    # последние вычисленные сроки (обновляются реже основной проверки)
    ssl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ssl_message: Mapped[str] = mapped_column(String(256), default="")
    domain_message: Mapped[str] = mapped_column(String(256), default="")
    expiry_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # какой порог напоминания уже отправлен (дней): None = ни одного / сброшено
    # после продления. Позволяет слать по одному алерту на каждый порог (эскалация).
    ssl_alerted_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_alerted_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # последний результат (для карточек/списка без обращения к тайм-серии)
    last_status: Mapped[str] = mapped_column(String(16), default="unknown")  # up|degraded|down|unknown
    last_message: Mapped[str] = mapped_column(String(512), default="")
    # разбивка последней проверки по IP (режим check_all_ips): [{ip,status,latency_ms,message}]
    last_ip_results: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # дни сертификата и т.п.
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # подряд неудачных проверок (для порога алерта и гистерезиса)
    consecutive_fails: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IgnoredDomain(Base):
    """Домен, который решили НЕ ставить на мониторинг.

    Обнаружение показывает всё, что агенты видят на веб-серверах, включая заведомо
    ненужное: дев-стенды, внутренние панели, парковки. Без этого списка плашка
    «найдено N вне мониторинга» висит всегда и перестаёт что-либо значить."""

    __tablename__ = "ignored_domains"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    by_user: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class CheckSample(Base):
    """Снимок результата проверки (тайм-серия для графиков/uptime)."""

    __tablename__ = "check_samples"
    __table_args__ = (Index("ix_check_samples_lookup", "check_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer)  # покрыт композитом (check_id, ts)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(String(16))  # up|degraded|down
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(String(512), default="")


class CheckIncident(Base):
    """Инцидент монитора: период «плохого» статуса (down/degraded) для uptime и алертов."""

    __tablename__ = "check_incidents"
    __table_args__ = (Index("ix_check_incidents_lookup", "check_id", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16))  # down | degraded
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # None = инцидент открыт
    last_message: Mapped[str] = mapped_column(String(512), default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)


class Location(Base):
    """Точка проверки — прокси (http/https/socks5), через который панель гоняет
    http-мониторы, чтобы видеть доступность сайта из разных сетей/регионов."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # прокси: http://user:pass@host:port | socks5://host:port
    url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AgentProbe(Base):
    """Последний результат локальной проверки сайта агентом (не тайм-серия).

    Сырьё, а не вердикт: агент сообщает код ответа, задержку, ошибку и нашлись ли
    ключевые слова, а оценивает это панель — теми же порогами и тем же кодом, что и
    обычные мониторы. Иначе логика инцидентов разъехалась бы по двум местам.
    """

    __tablename__ = "agent_probes"

    check_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    code: Mapped[int] = mapped_column(Integer, default=0)  # 0 = запрос не состоялся
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(String(512), default="")
    # Тело наружу не отдаём: агент сам смотрит ключевые слова и шлёт только «да/нет».
    # Так панель не хранит и не логирует содержимое закрытых страниц.
    kw_up_found: Mapped[bool] = mapped_column(Boolean, default=True)
    kw_down_found: Mapped[bool] = mapped_column(Boolean, default=False)


class LocationResult(Base):
    """Последний результат проверки монитора через конкретную локацию (не тайм-серия)."""

    __tablename__ = "location_results"
    __table_args__ = (
        Index("ix_location_results_lookup", "check_id", "location_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer, index=True)
    location_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16))  # up|degraded|down
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(String(512), default="")
    # подряд неудачных зондов из этой локации — для дебаунса локационных алертов
    # (флапающая прокси не должна слать алерт на каждый транзиентный таймаут)
    consecutive_fails: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Server(Base):
    """Нода, шлющая метрики агентом (push, исходящий HTTPS). Панель НЕ хранит доступа
    к серверу — только хеш токена агента (валидирует входящие репорты)."""

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    group_name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), index=True)  # sha256 токена
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # сообщается агентом
    hostname: Mapped[str] = mapped_column(String(255), default="")
    os: Mapped[str] = mapped_column(String(128), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    # целевая версия агента: если задана и != текущей, панель просит агент обновиться
    # (агент ставит ТОЛЬКО подписанное и более новое). Пусто = не трогать.
    target_agent_version: Mapped[str] = mapped_column(String(32), default="")
    local_ip: Mapped[str] = mapped_column(String(64), default="")  # сообщает агент
    external_ip: Mapped[str] = mapped_column(String(64), default="")  # источник запроса
    # IP, с которого агент будет ходить В панель (опционально): панель ведёт
    # data/agent_allow_ips, а хостовый скрипт (ops/agent-firewall-sync.sh)
    # разрешает эти адреса в ufw/firewalld, если панель закрыта фаерволом
    agent_ip: Mapped[str] = mapped_column(String(64), default="")
    # полный последний снимок (диски/swap/uptime/проверки) — JSON
    last_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # когда последний раз положили строку в server_metrics. Отчёты приходят
    # чаще, чем нужно графикам, и без этой отметки история пухла бы вчетверо
    metric_written_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # пороги алертов (%, 0 = выкл) и таймаут оффлайна
    cpu_alert_percent: Mapped[int] = mapped_column(Integer, default=90)
    mem_alert_percent: Mapped[int] = mapped_column(Integer, default=90)
    disk_alert_percent: Mapped[int] = mapped_column(Integer, default=90)  # «проблема»
    disk_warn_percent: Mapped[int] = mapped_column(Integer, default=85)  # «предупреждение»
    disk_crit_percent: Mapped[int] = mapped_column(Integer, default=95)  # «критично»
    temp_alert_c: Mapped[int] = mapped_column(Integer, default=0)  # порог темп. CPU, °C (0=выкл)
    conntrack_alert_percent: Mapped[int] = mapped_column(Integer, default=90)  # заполнение conntrack, % (0=выкл)
    # Занятость слотов подключений СУБД, % (0=выкл). Отдельный порог: коннекты
    # кончаются задолго до того, как что-то видно по CPU/памяти самой базы.
    db_conn_alert_percent: Mapped[int] = mapped_column(Integer, default=85)
    # За сколько дней до истечения предупреждать о сертификатах кластера и
    # токенах Flux (0=выкл). Две недели — чтобы успеть спланировать ротацию.
    kube_expiry_alert_days: Mapped[int] = mapped_column(Integer, default=14)
    # Глубина очереди RabbitMQ, с которой алертим. 0 = выключено для всей ноды.
    queue_alert_depth: Mapped[int] = mapped_column(Integer, default=0)
    # Переопределения по КОНКРЕТНЫМ очередям: {"<источник>|<vhost>/<имя>": порог},
    # 0 = эту очередь не алертить. Источник в ключе обязателен: на ноде бывает
    # несколько инстансов RabbitMQ (dev/stage), и имена очередей в них совпадают.
    queue_alert_over: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    disk_temp_alert_c: Mapped[int] = mapped_column(Integer, default=0)  # порог темп. диска, °C (0=выкл)
    # типы алертов, заглушённые ДЛЯ ЭТОГО сервера (переопределяют глобальные правила)
    alert_mutes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    offline_after_seconds: Mapped[int] = mapped_column(Integer, default=120)
    # сколько секунд превышение (CPU/RAM/темп/conntrack/темп диска) должно ДЕРЖАТЬСЯ
    # подряд, прежде чем слать алерт — гасит кратковременные спайки. По умолч. 15 мин.
    alert_sustain_seconds: Mapped[int] = mapped_column(Integer, default=900)
    # момент последней перезагрузки (аптайм упал) — для алерта «перезагружен»
    rebooted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # дедуп активных алертов сервера: ключ → уровень (0 ok, disk 1/2/3), + reboot_at
    alert_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # быстрый снуз: не слать алерты по этому серверу до этого момента (None = не
    # заглушён). Авто-истекает.
    snooze_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # точечный временный снуз ОТДЕЛЬНЫХ типов алертов: {тип: ISO-время «до»}.
    # Позволяет заглушить, напр., OOM на день, не теряя offline/CPU/диск. Истёкшие
    # записи игнорируются (и вычищаются при следующей записи снуза).
    alert_snoozes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # заглушённые репозитории бэкап-сервера (по имени): разовые/неактуальные — не
    # считаем проблемой, не алертим на устаревание, показываем приглушённо.
    backup_repo_mutes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # по умолчанию у каждого сервера ДОЛЖЕН быть бэкап; если не настроен — алерт.
    # Флаг «бэкап не требуется» (галочка в панели) снимает этот алерт для сервера.
    backup_not_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # «дампы СУБД настроены отдельно» — снимает пункт аудита с главной
    db_dumps_ok: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # приглушённые находки аудита покрытия — ключи вида "db:RabbitMQ", "kube_vol:/mnt/x".
    # Точечная альтернатива db_dumps_ok: «эту базу бэкапить не нужно», не глуша остальные.
    backup_audit_mutes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # «ночное окно» бэкапа: до какого часа он должен закончиться (8 = до 8:00). Если бэкап
    # ещё идёт или завершился позже — панель показывает МЯГКОЕ уведомление (не Telegram-алерт).
    # backup_anytime снимает проверку: некоторые сервисы бэкапят днём, и это норма.
    backup_deadline_hour: Mapped[int] = mapped_column(Integer, default=8, server_default="8")
    backup_anytime: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # OOM-киллы копим кумулятивно на ingest'е (дельта в отчёте живёт лишь один тик —
    # collector-поллинг её пропускал). Алерт шлём по high-water mark (см. alert_state
    # oom_seen), поэтому ни один килл не теряется. oom_victim — последняя жертва (имя
    # процесса из kmsg, best-effort от агента; пусто, если агент не смог прочитать).
    oom_total: Mapped[int] = mapped_column(Integer, default=0)
    oom_victim: Mapped[str] = mapped_column(String(200), default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ServerMetric(Base):
    """Лёгкая тайм-серия для графиков: cpu% / mem% / load1 (детали — в last_report)."""

    __tablename__ = "server_metrics"
    __table_args__ = (Index("ix_server_metrics_lookup", "server_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)  # покрыт композитом (server_id, ts)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # худший маунт
    load1: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_rx: Mapped[float | None] = mapped_column(Float, nullable=True)  # байт/сек
    net_tx: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_read: Mapped[float | None] = mapped_column(Float, nullable=True)  # байт/сек
    disk_write: Mapped[float | None] = mapped_column(Float, nullable=True)
    disk_read_iops: Mapped[float | None] = mapped_column(Float, nullable=True)  # операций/сек
    disk_write_iops: Mapped[float | None] = mapped_column(Float, nullable=True)
    # разбивка CPU (%) и память (%): для стек-графиков
    cpu_user: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_system: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_iowait: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_irq: Mapped[float | None] = mapped_column(Float, nullable=True)
    # per-core загрузка (JSON [%]) + частота/температура/троттлинг (null = нет датчика)
    cpu_cores_pct: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cpu_freq: Mapped[float | None] = mapped_column(Float, nullable=True)  # МГц
    cpu_temp: Mapped[float | None] = mapped_column(Float, nullable=True)  # °C
    cpu_throttle: Mapped[float | None] = mapped_column(Float, nullable=True)  # событий/интервал
    oom_kill: Mapped[float | None] = mapped_column(Float, nullable=True)  # OOM-киллов/интервал
    mem_cache: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_free: Mapped[float | None] = mapped_column(Float, nullable=True)
    # swap-активность (байт/сек) + детальная разбивка памяти (байты)
    swap_in: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_slab: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_dirty: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_writeback: Mapped[float | None] = mapped_column(Float, nullable=True)
    # заполнение по каждому маунту: [{"mount": "/", "pct": 53.0}, …] — для графика дисков
    disks: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # разбивка по интерфейсам/устройствам (для overlay-графиков)
    net_ifaces: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{if,rx,tx}]
    disk_devs: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{dev,util,await,temp}]
    # conntrack + сокеты
    conntrack_count: Mapped[float | None] = mapped_column(Float, nullable=True)
    conntrack_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    sock_used: Mapped[float | None] = mapped_column(Float, nullable=True)
    sock_tcp: Mapped[float | None] = mapped_column(Float, nullable=True)
    sock_tcp_tw: Mapped[float | None] = mapped_column(Float, nullable=True)
    sock_udp: Mapped[float | None] = mapped_column(Float, nullable=True)


class LocationSample(Base):
    """Тайм-серия проверок монитора через прокси-локацию (для графика по локации)."""

    __tablename__ = "location_samples"
    __table_args__ = (
        Index("ix_location_samples_lookup", "check_id", "location_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer)  # покрыт композитом (check_id, location_id, ts)
    location_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CheckIpSample(Base):
    """Тайм-серия проверки монитора по конкретному IP (режим «все адреса») — для
    графика времени ответа отдельно по каждому адресу."""

    __tablename__ = "check_ip_samples"
    __table_args__ = (Index("ix_check_ip_samples_lookup", "check_id", "ip", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer)
    ip: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DockerCommand(Base):
    """Очередь docker-действий (агент забирает в ответе на отчёт, исполняет через
    read-only proxy, постит результат). action: restart/stop/start/logs. Логи —
    разовый tail, храним недолго (прунится). Никакого фонового сбора логов."""

    __tablename__ = "docker_commands"
    __table_args__ = (Index("ix_docker_commands_lookup", "server_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    container: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(16))  # restart/stop/start/logs
    tail: Mapped[int] = mapped_column(Integer, default=200)  # строк для logs (если since=0)
    since: Mapped[int] = mapped_column(Integer, default=0)  # logs за последние N сек (0=tail)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[str] = mapped_column(Text, default="")  # вывод логов / текст ошибки
    ok: Mapped[bool | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KubeCommand(Base):
    """Очередь kube-действий (агент забирает, исполняет через kube-api по токену
    узкого SA, постит результат). action: rollout_restart/delete_pod/logs. Логи —
    разовый tail, храним недолго (прунится вместе с docker-командами)."""

    __tablename__ = "kube_commands"
    __table_args__ = (Index("ix_kube_commands_lookup", "server_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    ns: Mapped[str] = mapped_column(String(253))
    kind: Mapped[str] = mapped_column(String(16), default="pod")  # deployment/statefulset/daemonset/pod
    name: Mapped[str] = mapped_column(String(253))
    action: Mapped[str] = mapped_column(String(20))  # rollout_restart/delete_pod/logs
    tail: Mapped[int] = mapped_column(Integer, default=400)
    since: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackupCommand(Base):
    """Очередь backup-действий (агент исполняет через узкий helper, постит результат).
    action: set_paths/set_schedule/run_now. Параметры — в JSON-полях."""

    __tablename__ = "backup_commands"
    __table_args__ = (Index("ix_backup_commands_lookup", "server_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(24))  # set_paths/set_schedule/run_now/provision/provision_client/deploy_tls_front/get_cert
    mode: Mapped[str] = mapped_column(String(10), default="exclude")
    paths: Mapped[list | None] = mapped_column(JSON, nullable=True)
    schedule: Mapped[str] = mapped_column(String(8), default="")
    # доп. параметры провижининга (repo_url/repopass/hpass/cacert/retention…). Несёт
    # секреты только пока команда pending/running — очищается при завершении.
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[str] = mapped_column(Text, default="")
    ok: Mapped[bool | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BackupSetupJob(Base):
    """Задача «настроить бэкап» — оркестрация нескольких backup-команд в фоне (агент
    поллит ~1с; провижининг долгий: caddy/restic). Фронт поллит статус. Секретов НЕ
    хранит (они живут в BackupCommand.payload и чистятся при завершении команды)."""

    __tablename__ = "backup_setup_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer, index=True)
    backup_server_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/error
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class OomEvent(Base):
    """Журнал OOM-киллов: одна запись на отчёт агента с киллом — когда и кого убило.
    victim = «comm (pid N)» из kmsg (может быть пусто, если ядро не дало имя),
    count = сколько киллов за интервал этого отчёта."""

    __tablename__ = "oom_events"
    __table_args__ = (Index("ix_oom_events_lookup", "server_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    victim: Mapped[str] = mapped_column(String(200), default="")
    count: Mapped[int] = mapped_column(Integer, default=1)


class BackupVaultItem(Base):
    """Сейф доступов к бэкапам: ОДИН зашифрованный блоб на репозиторий.

    Панель хранит ТОЛЬКО шифротекст: ключ выводится из vault-пароля в браузере
    (PBKDF2 → AES-GCM), на сервер пароль не уходит и здесь не кэшируется. Свойство
    «БД бесполезна для атакующего» сохраняется: без vault-пароля это шум. Внутри
    блоба — URL репозитория, пароль, CA-серт и всё, что нужно для восстановления.
    """

    __tablename__ = "backup_vault"
    __table_args__ = (UniqueConstraint("repo", name="uq_backup_vault_repo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(200), index=True)  # имя репозитория = ключ
    server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # чей это бэкап
    server_name: Mapped[str] = mapped_column(String(200), default="")  # для показа без джойна
    nonce: Mapped[str] = mapped_column(String(64))  # base64, свой на каждую запись
    ciphertext: Mapped[str] = mapped_column(Text)   # base64(AES-GCM)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
