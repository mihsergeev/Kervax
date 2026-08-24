from fastapi import APIRouter, Body, HTTPException, status

from app import audit, backup, settings_store
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.schemas import BackupConfig, BackupFileInfo, RestoreResult

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/config", response_model=BackupConfig)
async def get_config(_: AdminUser, session: SessionDep) -> BackupConfig:
    cfg = await settings_store.get_backup_config(session, get_settings())
    return BackupConfig(**cfg)


@router.put("/config", response_model=BackupConfig)
async def put_config(
    body: BackupConfig, user: AdminUser, session: SessionDep
) -> BackupConfig:
    await settings_store.set_backup_config(session, body.interval_hours, body.keep)
    await audit.record(session, user.username, "backup_config", "")
    cfg = await settings_store.get_backup_config(session, get_settings())
    return BackupConfig(**cfg)


@router.get("/export")
async def export_now(_: AdminUser, session: SessionDep) -> dict:
    """Свежий бэкап конфигурации (без метрик) — фронт сохранит как файл."""
    return await backup.export_data(session)


@router.get("/list", response_model=list[BackupFileInfo])
async def list_backups(_: AdminUser) -> list[BackupFileInfo]:
    return [BackupFileInfo(**f) for f in backup.list_files(get_settings())]


@router.get("/file/{name}")
async def get_file(name: str, _: AdminUser) -> dict:
    try:
        return backup.read_file(get_settings(), name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/run", response_model=BackupFileInfo)
async def run_now(user: AdminUser, session: SessionDep) -> BackupFileInfo:
    """Создать файл автобэкапа на сервере прямо сейчас."""
    settings = get_settings()
    cfg = await settings_store.get_backup_config(session, settings)
    name = await backup.write_auto_backup(session, settings, cfg["keep"])
    await audit.record(session, user.username, "backup_run", name)
    info = next((f for f in backup.list_files(settings) if f["name"] == name), None)
    return BackupFileInfo(**info)


@router.post("/restore", response_model=RestoreResult)
async def restore(
    user: AdminUser, session: SessionDep, data: dict = Body(...)
) -> RestoreResult:
    try:
        counts = await backup.import_data(session, data, get_settings())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await audit.record(session, user.username, "backup_restore", "")
    return RestoreResult(restored=counts)
