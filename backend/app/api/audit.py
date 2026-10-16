from fastapi import APIRouter, Query
from sqlalchemy import select

from app import audit
from app.deps import AdminUser, SessionDep
from app.models import AuditLog
from app.schemas import AuditEntryOut

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEntryOut])
async def list_audit(
    _: AdminUser,
    session: SessionDep,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[AuditEntryOut]:
    rows = await session.scalars(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    )
    return await audit.readable(session, list(rows))
