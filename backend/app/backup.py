"""Экспорт/импорт конфигурации панели в JSON — БЕЗ тяжёлых тайм-серий (метрик).

В бэкап попадают мониторы, инциденты, локации, серверы (агенты), связи между серверами,
окна обслуживания, скрытые домены, сейф бэкапов (зашифрован паролем сейфа, без него это шум)
и настройки. НЕ попадают: check_samples / location_samples / server_metrics (метрики),
история алертов, логи аудита и пользователи (админ восстанавливается из env при старте).

Восстановление трогает только таблицы, которые есть в файле: старый файл без связей или сейфа
их не сотрет.

Файл (выгрузка и автобэкап на диске) зашифрован: в мониторах лежат пароли и заголовки с
токенами, в настройках токены ботов, а файл уезжает на ноутбук админа и в копии диска.
Ключ выводится из KERVAX_BACKUP_KEY, если он задан, иначе из KERVAX_JWT_SECRET. Чтобы
поднять файл на другой панели, ей нужен тот же ключ: KERVAX_BACKUP_KEY = значение
KERVAX_BACKUP_KEY (или KERVAX_JWT_SECRET) старой панели. Старые открытые файлы
восстанавливаются как раньше.
"""

import base64
import hashlib
import json
import os
import re
import zlib
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sqlalchemy import DateTime, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AppSetting,
    BackupVaultItem,
    Check,
    CheckIncident,
    IgnoredDomain,
    Location,
    MaintenanceWindow,
    Server,
    ServerLink,
)

FORMAT_VERSION = 1

# Порядок = порядок вставки при восстановлении.
_MODELS = [
    ("locations", Location),
    ("checks", Check),
    ("check_incidents", CheckIncident),
    ("servers", Server),
    ("server_links", ServerLink),
    ("maintenance_windows", MaintenanceWindow),
    ("ignored_domains", IgnoredDomain),
    ("backup_vault", BackupVaultItem),
    ("app_settings", AppSetting),
]

# поля-снимок метрик у серверов — обнуляем (это метрики, не конфиг)
_SERVER_METRIC_FIELDS = ("last_report", "last_seen")
# у связи между серверами - последние попытки и замер, агент пришлет новые
_LINK_STATE_FIELDS = {"win": None, "last_ts": None, "last_loss": None, "last_rtt": None, "last_err": ""}

_NAME_RE = re.compile(r"^kervax-backup-\d{8}-\d{6}\.json$")


_ENC_ALG = "hkdf-sha256+zlib+aes-256-gcm"
_ENC_AAD = b"kervax-panel-backup"


def _derive(material: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"kervax-backup", info=b"panel-backup-v1"
    ).derive(material.encode("utf-8"))


def _key_id(key: bytes) -> str:
    # по отпечатку видно, каким ключом закрыт файл, без раскрытия самого ключа
    return hashlib.sha256(key).hexdigest()[:12]


def _keys(settings: Settings) -> list[bytes]:
    """Ключи по порядку: первым шифруем, расшифровать пробуем каждым. JWT-секрет вторым,
    чтобы файлы, сделанные до того, как задали KERVAX_BACKUP_KEY, тоже открывались."""
    out: list[bytes] = []
    for m in (settings.backup_key, settings.jwt_secret):
        if m and (k := _derive(m)) not in out:
            out.append(k)
    return out


def seal(data: dict, settings: Settings) -> dict:
    """Шифрует выгрузку целиком. Снаружи только то, по чему файл узнают: app, format, дата."""
    key = _keys(settings)[0]
    nonce = os.urandom(12)
    raw = zlib.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"), 6)
    ct = AESGCM(key).encrypt(nonce, raw, _ENC_AAD)
    return {
        "app": "kervax",
        "format": FORMAT_VERSION,
        "created_at": data.get("created_at"),
        "encrypted": {
            "alg": _ENC_ALG,
            "key_id": _key_id(key),
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(ct).decode(),
        },
    }


def unseal(data: dict, settings: Settings) -> dict:
    """Открытый файл возвращает как есть, зашифрованный расшифровывает ключом этой панели."""
    enc = data.get("encrypted") if isinstance(data, dict) else None
    if enc is None:
        return data
    if not isinstance(enc, dict) or enc.get("alg") != _ENC_ALG:
        raise ValueError("Неизвестный формат шифрования бэкапа")
    try:
        nonce = base64.b64decode(enc["nonce"])
        ct = base64.b64decode(enc["data"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Файл бэкапа поврежден")
    for key in _keys(settings):
        try:
            raw = AESGCM(key).decrypt(nonce, ct, _ENC_AAD)
        except InvalidTag:
            continue
        return json.loads(zlib.decompress(raw))
    raise ValueError(
        f"Файл зашифрован ключом другой панели (отпечаток {enc.get('key_id', '?')}). "
        "Впишите в .env этой панели KERVAX_BACKUP_KEY со значением KERVAX_BACKUP_KEY "
        "или KERVAX_JWT_SECRET той панели, где файл сделан, и перезапустите ее"
    )


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
            if model is ServerLink:
                d.update(_LINK_STATE_FIELDS)
            serialized.append(d)
        tables[name] = serialized
    return {
        "app": "kervax",
        "format": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
    }


async def export_sealed(session: AsyncSession, settings: Settings) -> dict:
    return seal(await export_data(session), settings)


async def import_data(session: AsyncSession, data: dict, settings: Settings) -> dict:
    if isinstance(data, dict) and data.get("app") == "kervax" and "encrypted" in data:
        data = unseal(data, settings)
    if not isinstance(data, dict) or data.get("app") != "kervax" or "tables" not in data:
        raise ValueError("Это не файл бэкапа Kervax")
    if data.get("format") != FORMAT_VERSION:
        raise ValueError(f"Несовместимая версия бэкапа: {data.get('format')}")
    tables = data["tables"]
    if not isinstance(tables, dict):
        raise ValueError("Это не файл бэкапа Kervax")
    # только таблицы из файла: в бэкапе старой версии нет связей, окон и сейфа - их не трогаем
    present = [(n, m) for n, m in _MODELS if isinstance(tables.get(n), list)]

    # чистим существующие (в обратном порядке) и вставляем заново
    for _name, model in reversed(present):
        await session.execute(delete(model))
    counts: dict = {}
    for name, model in present:
        rows = tables[name]
        for row in rows:
            session.add(model(**_deserialize(row, model)))
        counts[name] = len(rows)

    # Postgres: после вставки явных id надо подвинуть sequence, иначе следующая
    # авто-вставка словит конфликт по primary key.
    if settings.db_url.startswith("postgresql"):
        for _name, model in present:
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


async def contents(session: AsyncSession) -> list[dict]:
    """Что попадет в бэкап: сколько записей в каждой таблице."""
    out = []
    for name, model in _MODELS:
        n = await session.scalar(select(func.count()).select_from(model))
        out.append({"key": name, "rows": int(n or 0)})
    return out


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
    data = await export_sealed(session, settings)
    d = backups_dir(settings)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"kervax-backup-{stamp}.json"
    # Через временный файл и rename: процесс, упавший посреди записи, оставлял обрезанный файл
    # под нормальным именем, он считался в keep и вытеснял целый. Временный под маску не
    # попадает, в списке его не видно
    final = os.path.join(d, name)
    tmp = f"{final}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    # прунинг старых
    files = [n for n in os.listdir(d) if _NAME_RE.match(n)]
    files.sort(reverse=True)
    for old in files[max(1, keep):]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    return name
