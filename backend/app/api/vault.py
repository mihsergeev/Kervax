"""Сейф доступов к бэкапам — хранилище ШИФРОТЕКСТА.

Ключевое свойство: панель не может прочитать содержимое сейфа. Ключ выводится из
vault-пароля в браузере (PBKDF2 → AES-GCM), на сервер пароль не передаётся и здесь
не кэшируется — бэкенд принимает и отдаёт непрозрачные блобы. Поэтому дамп базы
(и её бэкап) без vault-пароля остаётся шумом, как и было до появления сейфа.

Сервер отвечает за три вещи: доступ только админам, журнал обращений и хранение.
Проверку пароля делает клиент — расшифровкой verifier'а из меты.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from app import audit, settings_store
from app.deps import AdminUser, SessionDep
from app.models import BackupVaultItem

router = APIRouter(prefix="/vault", tags=["vault"])


class VaultMeta(BaseModel):
    """Параметры KDF и маркер проверки пароля. Секретов не содержит: соль публична
    по определению, verifier — шифротекст известной строки."""

    salt: str = Field(default="", max_length=128)  # base64
    iterations: int = 0
    verifier_nonce: str = Field(default="", max_length=64)
    verifier: str = Field(default="", max_length=512)


class VaultItemIn(BaseModel):
    repo: str = Field(min_length=1, max_length=200)
    server_id: int | None = None
    server_name: str = Field(default="", max_length=200)
    nonce: str = Field(min_length=1, max_length=64)
    ciphertext: str = Field(min_length=1, max_length=200_000)


class VaultItemOut(BaseModel):
    repo: str
    server_id: int | None
    server_name: str
    nonce: str
    ciphertext: str
    updated_at: str


@router.get("/meta", response_model=VaultMeta)
async def get_meta(_: AdminUser, session: SessionDep) -> VaultMeta:
    """Пустая мета = сейф ещё не заведён (фронт предложит задать пароль)."""
    return VaultMeta(**(await settings_store.get_vault_meta(session)))


@router.put("/meta", response_model=VaultMeta)
async def put_meta(body: VaultMeta, user: AdminUser, session: SessionDep) -> VaultMeta:
    """Завести сейф или сменить пароль. Смена пароля перешифровывает записи на
    клиенте и присылает их следом — поэтому мету и записи пишем в одном порядке:
    сначала мета, потом перезалив записей."""
    cur = await settings_store.get_vault_meta(session)
    await settings_store.set_vault_meta(session, body.model_dump())
    await audit.record(
        session, user.username, "vault_setup", "смена пароля" if cur.get("salt") else "создан"
    )
    return VaultMeta(**(await settings_store.get_vault_meta(session)))


@router.get("", response_model=list[VaultItemOut])
async def list_items(_: AdminUser, session: SessionDep) -> list[VaultItemOut]:
    """Отдаём шифротекст целиком: расшифровка идёт в браузере. Факт ЧТЕНИЯ списка не
    журналируем — это ещё не доступ к секрету; расшифровку фиксирует /opened."""
    rows = await session.scalars(select(BackupVaultItem).order_by(BackupVaultItem.repo))
    return [
        VaultItemOut(
            repo=r.repo, server_id=r.server_id, server_name=r.server_name,
            nonce=r.nonce, ciphertext=r.ciphertext,
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
        )
        for r in rows
    ]


@router.put("", response_model=dict)
async def upsert_items(
    body: list[VaultItemIn], user: AdminUser, session: SessionDep
) -> dict:
    if len(body) > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "слишком много записей за раз")
    existing = {
        r.repo: r for r in await session.scalars(select(BackupVaultItem))
    }
    for it in body:
        row = existing.get(it.repo)
        if row is None:
            row = BackupVaultItem(repo=it.repo)
            session.add(row)
        row.server_id = it.server_id
        row.server_name = it.server_name
        row.nonce = it.nonce
        row.ciphertext = it.ciphertext
    await session.commit()
    await audit.record(session, user.username, "vault_store", f"записей: {len(body)}")
    return {"saved": len(body)}


@router.delete("/{repo}", response_model=dict)
async def delete_item(repo: str, user: AdminUser, session: SessionDep) -> dict:
    await session.execute(sa_delete(BackupVaultItem).where(BackupVaultItem.repo == repo))
    await session.commit()
    await audit.record(session, user.username, "vault_delete", repo)
    return {"deleted": repo}


@router.post("/opened", response_model=dict)
async def mark_opened(user: AdminUser, session: SessionDep, repo: str = "") -> dict:
    """Клиент сообщает, что расшифровал сейф. Сам факт доступа к ключам от бэкапов
    должен оставаться в журнале — иначе просмотр секретов был бы невидим."""
    await audit.record(session, user.username, "vault_open", repo or "весь сейф")
    return {"ok": True}


@router.post("/reset", response_model=dict)
async def reset_vault(user: AdminUser, session: SessionDep) -> dict:
    """Забыт vault-пароль. Расшифровать содержимое не может НИКТО — ни панель, ни мы:
    ключ выводился только из пароля. Поэтому единственный путь — стереть сейф и
    собрать заново с нод. Сами бэкапы при этом целы: пароли репозиториев лежат на
    клиенте (env бэкапа) и на бэкап-сервере (prune-env), панель их оттуда и берёт.
    """
    n = await session.scalar(select(func.count()).select_from(BackupVaultItem)) or 0
    await session.execute(sa_delete(BackupVaultItem))
    await settings_store.set_vault_meta(session, {})
    await session.commit()
    await audit.record(session, user.username, "vault_reset", f"стёрто записей: {n}")
    return {"deleted": n}
