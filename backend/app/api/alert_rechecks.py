"""Fresh verification only: neither repairs nor dismissals."""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app import alert_recheck as recheck, audit, probe_scope
from app.collector import _aware
from app.deps import CurrentUser, SessionDep, group_allowed, user_sections
from app.models import AlertRecheck, Check, ProbeRequest, Server, User
from app.security import hash_agent_token
from app.agent_auth import require_agent_source

router = APIRouter(prefix="/alert-rechecks", tags=["alert rechecks"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])


class RecheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: Literal["server", "check"]
    object_id: int = Field(gt=0)
    kind: str = Field(min_length=1, max_length=64)


async def _object(session, user, typ: str, oid: int, sec: str, lock=False):
    if sec not in user_sections(user):
        raise HTTPException(403, "Раздел недоступен для этой учетной записи")
    model = Check if typ == "check" else Server
    q = select(model).where(model.id == oid)
    if lock:
        q = q.with_for_update()
    obj = await session.scalar(q)
    if obj is None or not group_allowed(user, obj.group_name, "sites" if typ == "check" else "servers"):
        raise HTTPException(404, "Объект не найден")
    return obj


async def _progress(session, job, now):
    recheck.expire(job, now)
    if job.status == "checking" and job.spec.get("probe"):
        req = await session.get(ProbeRequest, job.spec["probe"])
        from app import manual_probe
        if req and req.ts is not None and not await manual_probe.batch_waiting(session, req):
            if not req.status or _aware(req.ts) < _aware(job.created_at):
                recheck.finish(job, "unavailable", "Не удалось проверить: нет свежего результата нужной проверки", now)
            else:
                recheck.finish(job, "fixed" if req.status == "up" else "remains",
                               "Исправление подтверждено свежей проверкой" if req.status == "up" else "Проблема осталась", now)


@router.post("")
async def start(body: RecheckIn, user: CurrentUser, session: SessionDep, request: Request,
                background: BackgroundTasks) -> dict:
    now = datetime.now(timezone.utc)
    sec = recheck.section(body.object_type, body.kind)
    # Serialize this account's admission check, then the object's deduplication.
    # A unique active_key also protects deployments/tests on SQLite.
    await session.scalar(select(User).where(User.id == user.id).with_for_update())
    obj = await _object(session, user, body.object_type, body.object_id, sec, lock=True)
    key = f"{body.object_type}:{body.object_id}:{body.kind}"
    job = await session.scalar(select(AlertRecheck).where(AlertRecheck.active_key == key).with_for_update())
    if job:
        await _progress(session, job, now)
        if job.status == "checking":
            await session.commit()
            return recheck.public(job)
    recent = await session.scalar(select(AlertRecheck).where(
        AlertRecheck.object_type == body.object_type, AlertRecheck.object_id == body.object_id,
        AlertRecheck.kind == body.kind, AlertRecheck.created_at >= now - timedelta(seconds=5),
    ).order_by(AlertRecheck.created_at.desc()).limit(1))
    if recent:
        raise HTTPException(429, "Проверка только что завершилась; повторите через 5 секунд", headers={"Retry-After": "5"})
    count = await session.scalar(select(func.count()).select_from(AlertRecheck).where(
        AlertRecheck.requested_by == user.username, AlertRecheck.created_at >= now - timedelta(minutes=1)))
    active = await session.scalar(select(func.count()).select_from(AlertRecheck).where(
        AlertRecheck.status == "checking", AlertRecheck.deadline > now))
    if count >= 40 or active >= 100:
        raise HTTPException(429, "Слишком много проверок, повторите через минуту")
    spec = recheck.spec_for(obj, body.kind)
    job = AlertRecheck(id=uuid4().hex, active_key=key, object_type=body.object_type, object_id=obj.id,
                       kind=body.kind, section=sec, requested_by=user.username, created_at=now,
                       deadline=now + timedelta(seconds=recheck.TIMEOUT), status="checking", spec=spec,
                       message="Проверяю")
    error = recheck.unavailable(obj, body.kind, spec, now)
    if error:
        recheck.finish(job, "unavailable", error, now)
    session.add(job)
    # Retention is bounded without a new scheduler.
    await session.execute(delete(AlertRecheck).where(AlertRecheck.created_at < now - timedelta(days=7)))
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        other = await session.scalar(select(AlertRecheck).where(AlertRecheck.active_key == key))
        if other:
            return recheck.public(other)
        raise
    if job.status == "checking" and isinstance(obj, Check) and probe_scope.uses_agent(obj):
        from app import checks as checks_exec
        from app.collector import seen_online
        srv = await session.get(Server, obj.probe_server_id) if obj.probe_server_id else None
        if (srv and group_allowed(user, srv.group_name) and seen_online(srv, now) and obj.type == "http"
                and probe_scope.server_allowed(obj, srv.id)):
            main = ProbeRequest(check_id=obj.id, server_id=srv.id, created_at=now, kind="home",
                                spec={"recheck": job.id})
            session.add(main)
            for path in checks_exec.extra_paths_of(obj):
                session.add(ProbeRequest(check_id=obj.id, server_id=srv.id, created_at=now, kind="home",
                                         url=path, spec={"recheck": job.id}))
            await session.flush()
            job.spec = {**spec, "probe": main.id}
            srv.probe_pending_at = now
        else:
            recheck.finish(job, "unavailable", "Не удалось проверить: агент не получил задание", now)
    await session.commit()
    await audit.record(session, user.username, "alert_recheck", obj.name, body.kind)
    if job.status == "checking" and body.object_type == "check" and not probe_scope.uses_agent(obj):
        background.add_task(recheck.panel_work, request.app.state.session_factory, job.id)
    return recheck.public(job)


@router.get("")
async def latest(user: CurrentUser, session: SessionDep, request: Request, background: BackgroundTasks,
                 job_ids: Annotated[list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]] | None,
                                    Query(alias="id", max_length=100)] = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    query = select(AlertRecheck)
    if job_ids is not None:
        query = query.where(AlertRecheck.id.in_(job_ids))
    else:
        query = query.where(AlertRecheck.created_at >= now - timedelta(days=1)).order_by(
            AlertRecheck.created_at.desc()).limit(500)
    rows = list(await session.scalars(query))
    objects = {}
    for typ, model in (("check", Check), ("server", Server)):
        ids = {j.object_id for j in rows if j.object_type == typ}
        if ids:
            for obj in await session.scalars(select(model).where(model.id.in_(ids))):
                if group_allowed(user, obj.group_name, "sites" if typ == "check" else "servers"):
                    objects[(typ, obj.id)] = obj
    out, seen = [], set()
    for job in rows:
        key = (job.object_type, job.object_id, job.kind)
        if job_ids is None and key in seen:
            continue
        seen.add(key)
        obj = objects.get((job.object_type, job.object_id))
        if obj is None or job.section not in user_sections(user):
            continue
        if job.status == "checking" and recheck.identity(obj) != job.spec["identity"]:
            recheck.finish(job, "unavailable", "Не удалось проверить: настройки объекта изменились", now)
        await _progress(session, job, now)
        if job.status == "checking" and job.object_type == "check" and not job.spec.get("probe"):
            if job.taken_at is None or (now - _aware(job.taken_at)).total_seconds() >= recheck.LEASE:
                background.add_task(recheck.panel_work, request.app.state.session_factory, job.id)
        out.append(recheck.public(job))
    await session.commit()
    return out


class ResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ok: bool
    snapshot: dict = Field(default_factory=dict)

    @field_validator("snapshot")
    @classmethod
    def bounded(cls, value):
        import json
        if len(json.dumps(value)) > 4_000_000:
            raise ValueError("слишком большой результат")
        return value


@agent_router.post("/alert-recheck-result")
async def result(body: ResultIn, session: SessionDep, request: Request, authorization: str = Header(default="")) -> dict:
    server = await session.scalar(select(Server).where(
        Server.token_hash == hash_agent_token(authorization.removeprefix("Bearer ").strip())
    ).with_for_update())
    if server is None or not server.enabled:
        raise HTTPException(401, "Неверный токен агента")
    await require_agent_source(server, request, session)
    await recheck.ingest(session, server, body.id, body.ok, body.snapshot, datetime.now(timezone.utc))
    await session.commit()
    return {"ok": True}
