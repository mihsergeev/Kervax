"""Хранилище редактируемых настроек панели (в БД, поверх env-дефолтов)."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AppSetting

ALERTS_KEY = "alerts"
MUTED_KEY = "alerts_muted"
UNCOVERED_KEY = "uncovered_digest_sent"
VAULT_META_KEY = "backup_vault_meta"  # соль/итерации/verifier сейфа (СЕКРЕТОВ НЕТ)
RETENTION_KEY = "retention"
BACKUP_KEY = "backup"
BACKUP_LAST_KEY = "backup_last"
SERVER_RULES_KEY = "server_alert_rules"
SITE_RULES_KEY = "site_alert_rules"

# Типы серверных алертов: ключ → (подпись, дефолтный шаблон текста).
# Плейсхолдеры: {server} {group} {value} {threshold} {severity}.
# Типы серверных алертов. Текст — только «суть» (detail); collector оборачивает
# дефолт в богатый формат «<иконка> «<сервер-ссылкой>» — <detail>» (как у сайтов).
# Плейсхолдеры: {server} {group} {value} {threshold} {severity} {streak} {victim}.
SERVER_ALERT_KINDS: dict[str, tuple[str, str]] = {
    "offline": ("Недоступен", "недоступен, агент не шлёт метрики"),
    "cpu": ("CPU", "CPU {value}% ≥ {threshold}%"),
    "mem": ("RAM", "RAM {value}% ≥ {threshold}%"),
    # без слова-серьёзности: уровень и так виден по ведущей иконке (⚠️/🔴/🚨),
    # а «(предупреждение)» только удлиняло строку. {severity} остаётся доступным
    # тем, кто пишет свой шаблон
    "disk": ("Диск", "диск {value}% ≥ {threshold}%"),
    "temp": ("Температура CPU", "CPU {value}°C ≥ {threshold}°C"),
    "throttle": ("Троттлинг CPU", "устойчивый тепловой троттлинг CPU ({streak} интервала подряд)"),
    "conntrack": ("Conntrack", "таблица conntrack заполнена на {value}% ≥ {threshold}%"),
    "db_conn": ("Коннекты СУБД", "{engine}: занято {used} из {limit} подключений ({value}% ≥ {threshold}%)"),
    "disktemp": ("Температура диска", "диск {value}°C ≥ {threshold}°C"),
    "reboot": ("Перезагрузка", "перезагружен (аптайм сброшен)"),
    "oom": ("OOM-killer", "OOM-kill: ядро убило {value} процесс(ов){victim} из-за нехватки памяти"),
    "docker_down": ("Docker: контейнер упал", "контейнер {container} не работает ({state}, restart-policy: {policy})"),
    "docker_loop": ("Docker: перезапуски", "контейнер {container} постоянно перезапускается ({restarts} рестарта за {window} мин)"),
    "backup_missing": ("Бэкап: не настроен", "на сервере не настроен бэкап (галочка «не требуется» снимает алерт)"),
    "backup_failed": ("Бэкап: ошибка", "последний бэкап завершился с ошибкой"),
    "backup_stale": ("Бэкап: не свежий", "бэкап не обновлялся {days} дн. (последний успех устарел)"),
    "backup_repo": ("Бэкап-сервер: репозитории", "репозитории требуют внимания (устарели/битые/залочены): {repos}"),
    "backup_dump": ("Бэкап: дамп СУБД не снимается", "дамп не обновляется, хотя бэкап идёт: {engines} (файловый снапшот живой базы может не восстановиться)"),
    "backup_dump_space": ("Бэкап: дамп пропущен, мало места", "дамп не снят из-за нехватки места (свободно {free}%): {engines} — расчистите раздел"),
    "backup_cron": ("Бэкап: дамп-CronJob не отрабатывает", "дамп-CronJob не отрабатывает (приостановлен / прогон упал / давно нет успешного дампа): {jobs}"),
    "backup_rotation": ("Бэкап-сервер: ротация встала",
                        "старые снапшоты не вычищаются: {repos}"),
    "queue": ("RabbitMQ: очередь переполнена",
              "очередь {queue} ({source}): {value} сообщений ≥ {threshold}"),
    "clock": ("Время: сдвиг часов", "часы разошлись на {value} с временем панели — ломает TOTP/TLS/корреляцию логов, синхронизируйте время"),
    # Дата — только половина новости; вторая строка говорит, чем это грозит и что
    # делать. «ИСТЁК 49 дн. назад» без пояснения вызывает не действие, а недоумение:
    # кластер-то работает.
    "kube_expiry": ("Kubernetes: истекает срок",
                    "{what} {where}\n↳ {value}, {date}{more}\n↳ {advice}"),
    # Первая строка отвечает на «что случилось», вторая — «где именно», третья — «куда
    # смотреть». Раньше это был сырой вывод Flux одной простынёй, и прочитать его можно
    # было, только зная Flux наизусть.
    "flux_down": ("Flux: доставка встала",
                  "доставка встала: {reason}\n↳ {what} {where}{more}\n↳ {message}{hint}"),    # Сводка, а не событие: одно сообщение раз в сутки про ноды, где появилось
    # новое (СУБД, кластер, веб-сервер, докер), а покрытия под это ещё нет. Тут
    # нечего «восстанавливать» — пункт закрывается прогоном плейбука.
    "uncovered": ("Появилось новое без покрытия",
                  "новое без покрытия мониторингом:\n{list}\n↳ поставить: {cmd}"),
}

# Прежние («Kervax: сервер …») дефолты серверных правил. Если в БД сохранён один
# из них — считаем его НЕ кастомным и рендерим новым богатым форматом (без «Kervax:»,
# имя сервера — ссылкой), а не гоняем старый текст. Иначе установки, где форму
# серверных алертов однажды сохранили, застряли бы на старом «Kervax:»-тексте.
LEGACY_SERVER_DEFAULTS: frozenset[str] = frozenset({
    "🖥 Kervax: сервер «{server}» — недоступен, агент не шлёт метрики",
    "🖥 Kervax: сервер «{server}» — CPU {value}% ≥ {threshold}%",
    "🖥 Kervax: сервер «{server}» — RAM {value}% ≥ {threshold}%",
    "🖥 Kervax: сервер «{server}» — диск {value}% ≥ {threshold}% ({severity})",
    "🌡 Kervax: сервер «{server}» — CPU {value}°C ≥ {threshold}°C",
    "🥵 Kervax: сервер «{server}» — устойчивый тепловой троттлинг CPU ({streak} интервала подряд)",
    "🔗 Kervax: сервер «{server}» — таблица conntrack заполнена на {value}% ≥ {threshold}%",
    "🌡 Kervax: сервер «{server}» — диск {value}°C ≥ {threshold}°C",
    # дефолт диска со словом «(предупреждение)» — до того, как уровень стал виден
    # по иконке; у кого он сохранён в БД, тот получит новый короткий текст
    "диск {value}% ≥ {threshold}% ({severity})",
    "🔄 Kervax: сервер «{server}» — перезагружен (аптайм сброшен)",
    "🧠 Kervax: сервер «{server}» — OOM-kill: ядро убило {value} процесс(ов){victim} из-за нехватки памяти",
})

# Типы алертов сайтов/мониторов. Плейсхолдеры: {name} {group} {message} {status}.
# Дефолт (текст == значению ниже) рендерится «богатым» форматом в collector:
#   «<хост> <иконка> — <имя-ссылкой на монитор> — <текст>» (см. _alert_text).
# Кастомный текст пользователя рендерится через .format() как обычная строка.
SITE_ALERT_KINDS: dict[str, tuple[str, str]] = {
    "down": ("Недоступен / деградация", "🔴 {name} — {message}"),
    "recovery": ("Восстановление", "✅ {name} — {message}"),
    "ssl": ("SSL-сертификат", "🔐 {name} — {message}"),
    "domain": ("Домен", "🌐 {name} — {message}"),
    "locpart": ("Частичная доступность (локации)", "🌍 {name}\n{message}"),
}

# Прежние (до-ребрендовые/«Kervax:») дефолты сайтовых правил. Если в БД сохранён
# один из них — считаем его НЕ кастомным и рендерим новым богатым форматом, а не
# гоняем старый шаблон. Иначе установки, где форму алертов однажды сохранили,
# застряли бы на старом тексте с «Kervax».
LEGACY_SITE_DEFAULTS: frozenset[str] = frozenset({
    "🔴 Kervax: «{name}» — недоступен: {message}",
    "✅ Kervax: монитор «{name}» снова в норме",
    "🔐 Kervax: «{name}» — {message}",
    "🌐 Kervax: «{name}» — {message}",
    "🌍 Kervax: «{name}»\n{message}",
})


async def get_raw(session: AsyncSession, key: str) -> str | None:
    return await _get_raw(session, key)


async def set_raw(session: AsyncSession, key: str, value: str) -> None:
    await _set_raw(session, key, value)


async def _get_raw(session: AsyncSession, key: str) -> str | None:
    return await session.scalar(
        select(AppSetting.value).where(AppSetting.key == key)
    )


async def _set_raw(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_alert_config(session: AsyncSession, settings: Settings) -> dict:
    """Эффективная конфигурация алертов: значение из БД, иначе — из env."""
    raw = await _get_raw(session, ALERTS_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "telegram_token": data.get("telegram_token") or settings.alert_telegram_token,
        "telegram_chat": data.get("telegram_chat") or settings.alert_telegram_chat,
        "telegram_api": (
            data.get("telegram_api") or settings.alert_telegram_api
        ).rstrip("/"),
        "webhook": data.get("webhook") or settings.alert_webhook,
        "flood_threshold": int(
            data.get("flood_threshold", settings.alert_flood_threshold)
        ),
    }


async def set_alert_config(
    session: AsyncSession,
    telegram_token: str,
    telegram_chat: str,
    webhook: str,
    telegram_api: str = "",
    flood_threshold: int = 6,
) -> None:
    await _set_raw(
        session,
        ALERTS_KEY,
        json.dumps(
            {
                "telegram_token": telegram_token.strip(),
                "telegram_chat": telegram_chat.strip(),
                "telegram_api": telegram_api.strip().rstrip("/"),
                "webhook": webhook.strip(),
                "flood_threshold": max(0, min(1000, int(flood_threshold))),
            }
        ),
    )


async def get_muted(session: AsyncSession) -> bool:
    """Глобальная пауза алертов (галочка «не слать»). По умолчанию False."""
    return await _get_raw(session, MUTED_KEY) == "1"


async def set_muted(session: AsyncSession, muted: bool) -> None:
    await _set_raw(session, MUTED_KEY, "1" if muted else "0")


async def get_uncovered_sent(session: AsyncSession) -> float:
    """Когда в последний раз уходила сводка о непокрытом (unix, 0 = никогда).

    Метка в БД, а не в памяти планировщика: рестарт контейнера не должен
    оборачиваться повторной сводкой, а два одинаковых сообщения подряд быстрее
    всего учат не читать сводку вовсе."""
    raw = await _get_raw(session, UNCOVERED_KEY)
    try:
        return float(raw or 0)
    except ValueError:
        return 0.0


async def set_uncovered_sent(session: AsyncSession, ts: float) -> None:
    await _set_raw(session, UNCOVERED_KEY, str(int(ts)))


async def get_retention(session: AsyncSession, settings: Settings) -> dict:
    """Сроки хранения тайм-серий (дней): значение из БД, иначе — из env."""
    raw = await _get_raw(session, RETENTION_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "server_days": int(
            data.get("server_days", settings.server_metric_retention_days)
        ),
        "sample_days": int(data.get("sample_days", settings.sample_retention_days)),
    }


async def set_retention(session: AsyncSession, server_days: int, sample_days: int) -> None:
    await _set_raw(
        session,
        RETENTION_KEY,
        json.dumps(
            {
                "server_days": max(1, min(3650, int(server_days))),
                "sample_days": max(1, min(3650, int(sample_days))),
            }
        ),
    )


async def get_backup_config(session: AsyncSession, settings: Settings) -> dict:
    """Настройки автобэкапа: интервал (часы, 0 = выкл) и сколько файлов хранить."""
    raw = await _get_raw(session, BACKUP_KEY)
    data = json.loads(raw) if raw else {}
    return {
        "interval_hours": int(
            data.get("interval_hours", settings.backup_interval_hours)
        ),
        "keep": int(data.get("keep", settings.backup_keep)),
    }


async def _get_rules(session: AsyncSession, key: str, kinds: dict) -> dict:
    """Правила алертов: kind → {enabled, text, scope_type, scope}. text пусто → дефолт."""
    raw = await _get_raw(session, key)
    data = json.loads(raw) if raw else {}
    out: dict = {}
    for kind, (_label, default_text) in kinds.items():
        r = data.get(kind) or {}
        st = r.get("scope_type")
        out[kind] = {
            "enabled": bool(r.get("enabled", True)),
            "text": (r.get("text") or "").strip() or default_text,
            "scope_type": st if st in ("all", "groups", "servers", "checks") else "all",
            "scope": r.get("scope") if isinstance(r.get("scope"), list) else [],
        }
    return out


async def _set_rules(session: AsyncSession, key: str, kinds: dict, rules: dict) -> None:
    clean: dict = {}
    for kind, (_label, default_text) in kinds.items():
        r = rules.get(kind) or {}
        st = r.get("scope_type")
        clean[kind] = {
            "enabled": bool(r.get("enabled", True)),
            "text": (str(r.get("text") or "").strip()[:500]) or default_text,
            "scope_type": st if st in ("all", "groups", "servers", "checks") else "all",
            "scope": r.get("scope") if isinstance(r.get("scope"), list) else [],
        }
    await _set_raw(session, key, json.dumps(clean, ensure_ascii=False))


async def get_server_alert_rules(session: AsyncSession) -> dict:
    return await _get_rules(session, SERVER_RULES_KEY, SERVER_ALERT_KINDS)


async def set_server_alert_rules(session: AsyncSession, rules: dict) -> None:
    await _set_rules(session, SERVER_RULES_KEY, SERVER_ALERT_KINDS, rules)


async def get_site_alert_rules(session: AsyncSession) -> dict:
    return await _get_rules(session, SITE_RULES_KEY, SITE_ALERT_KINDS)


async def set_site_alert_rules(session: AsyncSession, rules: dict) -> None:
    await _set_rules(session, SITE_RULES_KEY, SITE_ALERT_KINDS, rules)


async def set_backup_config(session: AsyncSession, interval_hours: int, keep: int) -> None:
    await _set_raw(
        session,
        BACKUP_KEY,
        json.dumps(
            {
                "interval_hours": max(0, min(24 * 30, int(interval_hours))),
                "keep": max(1, min(365, int(keep))),
            }
        ),
    )


async def get_vault_meta(session: AsyncSession) -> dict:
    """Параметры сейфа доступов. Пусто = сейф не заведён. Соль и verifier секретами
    не являются: без vault-пароля из них ничего не выводится."""
    raw = await _get_raw(session, VAULT_META_KEY)
    d = json.loads(raw) if raw else {}
    return {
        "salt": d.get("salt", ""),
        "iterations": int(d.get("iterations", 0) or 0),
        "verifier_nonce": d.get("verifier_nonce", ""),
        "verifier": d.get("verifier", ""),
    }


async def set_vault_meta(session: AsyncSession, meta: dict) -> None:
    await _set_raw(session, VAULT_META_KEY, json.dumps(meta))
