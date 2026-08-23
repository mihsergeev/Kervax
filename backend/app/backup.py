"""Экспорт/импорт конфигурации панели в JSON — БЕЗ тяжёлых тайм-серий (метрик).

В бэкап попадают мониторы, инциденты, локации, серверы (агенты) и настройки.
НЕ попадают: check_samples / location_samples / server_metrics (метрики),
логи аудита и пользователи (админ восстанавливается из env при старте).
"""

import json
import os
import re
from datetime import datetime, timezone

from sqlalchemy import DateTime, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AppSetting, Check, CheckIncident, Location, Server

FORMAT_VERSION = 1

# Порядок = порядок вставки при восстановлении.
_MODELS = [
    ("locations", Location),
    ("checks", Check),
    ("check_incidents", CheckIncident),
    ("servers", Server),
    ("app_settings", AppSetting),
]

# поля-снимок метрик у серверов — обнуляем (это метрики, не конфиг)
_SERVER_METRIC_FIELDS = ("last_report", "last_seen")

_NAME_RE = re.compile(r"^kervax-backup-\d{8}-\d{6}\.json$")


def _cols(model) -> list:
    return list(model.__table__.columns)


def _serialize(obj, model) -> dict:
    row: dict = {}
    for c in _cols(model):
        val = getattr(obj, c.name)
        if isinstance(val, datetime):
            val = val.isoformat()
        row[c.name] = val
    return row


def _deserialize(row: dict, model) -> dict:
    cols = {c.name: c for c in _cols(model)}
    out: dict = {}
    for k, v in row.items():
        col = cols.get(k)
        if col is None:
            continue  # неизвестное поле — игнорируем (совместимость версий)
        if v is not None and isinstance(col.type, DateTime):
            v = datetime.fromisoformat(v)
        out[k] = v
    return out


async def export_data(session: AsyncSession) -> dict:
    tables: dict = {}
    for name, model in _MODELS:
        rows = list(await session.scalars(select(model)))
        serialized = []
        for r in rows:
            d = _serialize(r, model)
            if model is Server:
                for f in _SERVER_METRIC_FIELDS:
                    d[f] = None
            serialized.append(d)
        tables[name] = serialized
    return {
        "app": "kervax",
        "format": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


async def import_data(session: AsyncSession, data: dict, settings: Settings) -> dict:
    if not isinstance(data, dict) or data.get("app") != "kervax" or "tables" not in data:
        raise ValueError("Это не файл бэкапа Kervax")
    if data.get("format") != FORMAT_VERSION:
        raise ValueError(f"Несовместимая версия бэкапа: {data.get('format')}")
    tables = data["tables"]

    # чистим существующие (в обратном порядке) и вставляем заново
    for _name, model in reversed(_MODELS):
        await session.execute(delete(model))
    counts: dict = {}
    for name, model in _MODELS:
        rows = tables.get(name) or []
        for row in rows:
            session.add(model(**_deserialize(row, model)))
        counts[name] = len(rows)

    # Postgres: после вставки явных id надо подвинуть sequence, иначе следующая
    # авто-вставка словит конфликт по primary key.
    if settings.db_url.startswith("postgresql"):
        for _name, model in _MODELS:
            if any(c.name == "id" for c in _cols(model)):
                tbl = model.__tablename__
                await session.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {tbl}), 1))"
                    )
                )
    await session.commit()
    return counts


# --- файлы автобэкапа на диске ---


def backups_dir(settings: Settings) -> str:
    return os.path.join(settings.data_dir, "backups")


def list_files(settings: Settings) -> list[dict]:
    d = backups_dir(settings)
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not _NAME_RE.match(name):
            continue
        st = os.stat(os.path.join(d, name))
        out.append(
            {
                "name": name,
                "size": st.st_size,
                "created_at": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc
                ).isoformat(),
            }
        )
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def read_file(settings: Settings, name: str) -> dict:
    if not _NAME_RE.match(name):
        raise ValueError("Недопустимое имя файла")
    path = os.path.join(backups_dir(settings), name)
    if not os.path.isfile(path):
        raise FileNotFoundError(name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def write_auto_backup(session: AsyncSession, settings: Settings, keep: int) -> str:
    """Создаёт файл бэкапа на диске и удаляет лишние (оставляет keep новейших)."""
    data = await export_data(session)
    d = backups_dir(settings)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"kervax-backup-{stamp}.json"
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    # прунинг старых
    files = [n for n in os.listdir(d) if _NAME_RE.match(n)]
    files.sort(reverse=True)
    for old in files[max(1, keep):]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    return name
