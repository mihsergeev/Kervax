"""Журнал ошибок панели (app/panel_errors.py): прием ошибок интерфейса и сообщений "сообщить о
проблеме" от любой учетки, просмотр и отметка "разобрано" - только админ."""

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

from app import audit
from app import panel_errors as pe
from app.deps import AdminUser, CurrentUser, SessionDep
from app.models import PanelError

router = APIRouter(prefix="/panel-errors", tags=["panel-errors"])

# ошибки интерфейса: столько записей в минуту с одной учетки из процесса, остальное молча
# отбрасывается (фронт сам ограничивает частоту, это страховка от зациклившейся вкладки)
FRONT_PER_MIN = 20
# сообщения о проблеме: столько в час с одной учетки
REPORTS_PER_HOUR = 10
_SHOWN = 300

_front_hits: dict[str, list[float]] = {}
_report_hits: dict[str, list[float]] = {}


def _hit(store: dict[str, list[float]], key: str, window: float, limit: int) -> bool:
    """Учесть попытку; False - лимит за окно исчерпан."""
    now = time.monotonic()
    xs = [x for x in store.get(key, []) if now - x < window]
    if len(xs) >= limit:
        store[key] = xs
        return False
    xs.append(now)
    store[key] = xs
    if len(store) > 1000:
        store.clear()
    return True


def reset_limits() -> None:
    _front_hits.clear()
    _report_hits.clear()


class FrontErrorIn(BaseModel):
    kind: str = Field(default="error", max_length=16)  # error | rejection | render
    message: str = Field(default="", max_length=2000)
    stack: str = Field(default="", max_length=8000)
    component: str = Field(default="", max_length=4000)
    page: str = Field(default="", max_length=300)
    section: str = Field(default="", max_length=32)


class ClientLogIn(BaseModel):
    ts: str = Field(default="", max_length=40)
    method: str = Field(default="", max_length=10)
    path: str = Field(default="", max_length=300)
    status: int = 0
    detail: str = Field(default="", max_length=500)


class ReportIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    page: str = Field(default="", max_length=300)
    section: str = Field(default="", max_length=32)
    client_log: list[ClientLogIn] = Field(default_factory=list, max_length=20)


class ResolveIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)
    resolved: bool = True


def _first_frame(stack: str) -> str:
    """Первый кадр стека ("at f (file.js:1:2)" у Chrome, "f@file.js:1:2" у Firefox): по нему одна
    и та же ошибка складывается в одну запись, а разные с одинаковым текстом - нет."""
    for ln in (stack or "").splitlines()[:8]:
        ln = ln.strip()
        if ln.startswith("at ") or "@" in ln:
            return ln[:200]
    return ""


@router.post("/front")
async def front_error(body: FrontErrorIn, user: CurrentUser, request: Request) -> dict:
    if not _hit(_front_hits, user.username, 60.0, FRONT_PER_MIN):
        return {"ok": True, "recorded": False}
    kind = body.kind if body.kind in ("error", "rejection", "render") else "error"
    detail = body.stack
    if body.component:
        detail = (detail + "\n\n" if detail else "") + "компонент:" + body.component
    rid = await pe.record(
        request.app.state.session_factory, kind="front", action=f"interface {kind}",
        message=body.message or "(без текста)", detail=detail, username=user.username,
        page=body.page, section=body.section, fp_extra=_first_frame(body.stack),
    )
    return {"ok": True, "recorded": rid is not None}


@router.post("/report")
async def report(body: ReportIn, user: CurrentUser, session: SessionDep, request: Request) -> dict:
    if not _hit(_report_hits, user.username, 3600.0, REPORTS_PER_HOUR):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много сообщений за час, попробуйте позже")
    recent = await pe.recent_for(session, user.username)
    attached = [pe.short_out(r) for r in recent]
    client = [pe.mask_obj(x.model_dump()) for x in body.client_log[-20:]]
    rid = await pe.record(
        request.app.state.session_factory, kind="report", action="report",
        message=body.text.strip(), username=user.username, page=body.page, section=body.section,
        extra={"errors": attached, "client_log": client}, dedupe=False,
    )
    if rid is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Не удалось сохранить сообщение")
    return {"id": rid, "attached": len(attached), "client_log": len(client)}


def _counts_q():
    return select(PanelError.kind, func.count()).where(PanelError.resolved.is_(False)).group_by(PanelError.kind)


@router.get("/count")
async def count(_: AdminUser, session: SessionDep) -> dict:
    by = {k: int(n) for k, n in (await session.execute(_counts_q())).all()}
    return {"open": sum(by.values()), "reports": by.get("report", 0)}


@router.get("")
async def list_errors(
    _: AdminUser,
    session: SessionDep,
    state: str = "open",
    kind: str = "",
    q: str = "",
    days: int = 30,
) -> dict:
    now = datetime.now(timezone.utc)
    days = max(1, min(days, 365))
    query = select(PanelError).where(PanelError.last_at >= now - timedelta(days=days))
    if state == "open":
        query = query.where(PanelError.resolved.is_(False))
    elif state == "resolved":
        query = query.where(PanelError.resolved.is_(True))
    if kind in pe.KINDS:
        query = query.where(PanelError.kind == kind)
    if q.strip():
        like = f"%{q.strip()[:100]}%"
        query = query.where(or_(PanelError.message.ilike(like), PanelError.action.ilike(like),
                                PanelError.username.ilike(like), PanelError.server_name.ilike(like),
                                PanelError.page.ilike(like), PanelError.detail.ilike(like)))
    rows = list(await session.scalars(query.order_by(PanelError.last_at.desc()).limit(_SHOWN + 1)))
    by = {k: int(n) for k, n in (await session.execute(_counts_q())).all()}
    day = await session.scalar(select(func.count()).select_from(PanelError).where(
        PanelError.last_at >= now - timedelta(days=1), PanelError.kind != "report")) or 0
    resolved = await session.scalar(select(func.count()).select_from(PanelError).where(
        PanelError.resolved.is_(True))) or 0
    return {
        "items": [pe.row_out(r) for r in rows[:_SHOWN]],
        "more": len(rows) > _SHOWN,
        "counts": {"open": sum(by.values()), "by_kind": by, "day": int(day), "resolved": int(resolved)},
        "keep_days": pe.KEEP.days,
        "max_rows": pe.MAX_ROWS,
    }


@router.post("/resolve")
async def resolve(body: ResolveIn, user: AdminUser, session: SessionDep) -> dict:
    now = datetime.now(timezone.utc)
    res = await session.execute(
        update(PanelError).where(PanelError.id.in_(body.ids)).values(
            resolved=body.resolved, resolved_at=now if body.resolved else None,
            resolved_by=user.username if body.resolved else "")
    )
    await session.commit()
    await audit.record(
        session, user.username, "panel_errors_resolve" if body.resolved else "panel_errors_reopen",
        f"{len(body.ids)}", ",".join(str(i) for i in body.ids[:30]),
    )
    return {"ok": True, "changed": res.rowcount or 0}
