"""Резервный сервер бэкапов (app/bsrv_mirror.py): галка "держать копию на резервном" у репозитория
сервера бэкапов, роль "резервный" у сервера, повторная отправка настроек и "забрать сейчас".

Все - только админу: это доступ одного сервера к бэкапам другого. Панель здесь только называет, что
копировать; удалить копию, сменить адрес известного основного или разморозить копию она не может."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app import audit, panel_errors
from app import bsrv_mirror as bm
from app.api.relay import _by_token
from app.api.servers import _bcmd_create, _bcmd_wait, _get_or_404, _out, _setup_tasks
from app.deps import AdminUser, SessionDep
from app.models import BackupCommand, Server
from app.schemas import BackupCommandOut, BackupMirrorRoleIn, BackupRepoMirrorIn, ServerOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/servers", tags=["backup-mirror"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])


async def _sync(factory, primary_ids: set[int], mirror_ids: set[int], user: str) -> None:
    """Разослать настройки: сначала основным (кому и что можно читать), дождаться их, потом
    резервным (откуда и что брать) - иначе первая перекачка уперлась бы в закрытую дверь.
    Итог каждой команды пишется в записи репозиториев основного (allow/set), провал - еще и в
    журнал ошибок панели."""
    async with factory() as s:
        servers = list(await s.scalars(select(Server)))
    byid = {x.id: x for x in servers}
    want = bm.plan(servers)
    now = datetime.now(timezone.utc)
    allow: list[tuple[int, int, int]] = []
    # резервные, с которыми основной связан по настройкам, по своему отчету или которых назвали
    # явно (только что сняли последний репозиторий - доступ надо убрать, а в настройках его уже нет)
    named = set(mirror_ids)
    for pid in sorted(primary_ids):
        p = byid.get(pid)
        if p is None:
            continue
        for mid in sorted({m for m, prim in want.items() if pid in prim} | bm.reported_mirror_ids(p, now) | named):
            m = byid.get(mid)
            repos = (want.get(mid) or {}).get(pid) or []
            if m is None and repos:
                continue
            payload = bm.allow_payload(m, repos, now) if m is not None else {"name": bm.mirror_name(mid), "repos": []}
            allow.append((pid, mid, await _bcmd_create(factory, pid, "mirror_allow", payload)))
            mirror_ids = mirror_ids | ({mid} if m is not None else set())
    results: dict[tuple[str, int, int], tuple[bool, str]] = {}
    for pid, mid, cid in allow:
        results[("allow", pid, mid)] = await _bcmd_wait(factory, cid, 120)
    sets = []
    for mid in sorted(mirror_ids):
        m = byid.get(mid)
        if m is None or not bm.is_mirror(m):
            continue
        sets.append((mid, await _bcmd_create(factory, mid, "mirror", bm.set_payload(mid, servers, now))))
    for mid, cid in sets:
        results[("set", 0, mid)] = await _bcmd_wait(factory, cid, 120)
    # Итог - в свое поле основного (backup_mirror_sync), не в backup_mirror: ручки меняют тот, пока
    # рассылка ждет агентов, и запись итога поверх затерла бы только что снятую копию
    ts = datetime.now(timezone.utc).isoformat()
    touched = {pid for (_k, pid, _m) in results if pid} | {
        pid for (k, _p, mid) in results if k == "set" for pid in (want.get(mid) or {})}
    async with factory() as s:
        for pid in touched:
            x = await s.get(Server, pid)
            if x is None:
                continue
            cur = {k: dict(v) for k, v in (x.backup_mirror_sync or {}).items() if isinstance(v, dict)}
            for (kind, kpid, mid), (ok, out) in results.items():
                if (kind == "allow" and kpid == pid) or (kind == "set" and pid in (want.get(mid) or {})):
                    cur.setdefault(str(mid), {})[kind] = {
                        "ok": ok, "out": (out or "").replace("\r", " ").strip()[:400], "ts": ts}
            x.backup_mirror_sync = cur
        await s.commit()
    for (kind, pid, mid), (ok, out) in results.items():
        if not ok:
            sid = pid if kind == "allow" else mid
            await panel_errors.record(
                factory, kind="command", action=f"backup mirror {kind}", message=(out or "команда не прошла")[:250],
                username=user, section="backups", server_id=sid,
                detail=f"основной {byid[pid].name if pid in byid else '-'}, резервный {byid[mid].name if mid in byid else mid}")


def _start_sync(request: Request, primary_ids: set[int], mirror_ids: set[int], user: str) -> None:
    t = asyncio.create_task(_sync(request.app.state.session_factory, primary_ids, mirror_ids, user))
    _setup_tasks.add(t)
    t.add_done_callback(_setup_tasks.discard)


def _repo_names(srv) -> set[str]:
    return {str(r.get("name")) for r in (((srv.last_report or {}).get("backup_server") or {}).get("repos") or [])
            if isinstance(r, dict) and r.get("name")}


@router.post("/{server_id}/backup/repo-mirror", response_model=ServerOut)
async def backup_repo_mirror(server_id: int, body: BackupRepoMirrorIn, request: Request, user: AdminUser,
                             session: SessionDep) -> ServerOut:
    """Держать копию репозитория на резервном сервере или перестать. Перестать - резервный больше не
    забирает новое, копия на нем остается (удалить ее можно только командой на нем самом)."""
    s = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    ent = dict(s.backup_mirror or {})
    old = ent.get(body.repo) if isinstance(ent.get(body.repo), dict) else None
    mirrors: set[int] = set()
    if body.mirror_id is not None:
        m = await _get_or_404(body.mirror_id, session, user)
        if m.id == s.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "копию держат на другом сервере, не на этом же")
        if not bm.is_mirror(m):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{m.name} не резервный сервер")
        why = bm.why_not_mirror(m, now) or bm.why_not_primary(s, now)
        if why:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, why)
        if body.repo not in _repo_names(s):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"на {s.name} нет репозитория {body.repo}")
        if old and old.get("mirror") == m.id:
            return _out(s, now)
        # имя основного на резервном: то же, что у других репозиториев этого сервера там, иначе из
        # имени сервера; совпало с другим основным того же резервного - с id
        src = next((e["src"] for e in bm.entries(s).values() if e["mirror"] == m.id), "")
        if not src:
            src = bm.src_name(s.name, s.id)
            others = list(await session.scalars(select(Server).where(Server.id != s.id, Server.backup_mirror.is_not(None))))
            if any(e["src"] == src and e["mirror"] == m.id for o in others for e in bm.entries(o).values()):
                src = f"{src}-{s.id}"
        ent[body.repo] = {"mirror": m.id, "src": src, "ts": now.isoformat(), "by": user.username}
        mirrors.add(m.id)
    elif old is None:
        return _out(s, now)
    else:
        ent.pop(body.repo, None)
    if old and isinstance(old.get("mirror"), int):
        mirrors.add(old["mirror"])
    s.backup_mirror = ent or None
    await session.commit()
    await session.refresh(s)
    await audit.record(session, user.username, "backup_repo_mirror" if body.mirror_id else "backup_repo_unmirror",
                       body.repo[:120], f"srv={server_id} mirror={body.mirror_id or (old or {}).get('mirror')}")
    _start_sync(request, {s.id}, mirrors, user.username)
    return _out(s, now)


@router.post("/{server_id}/backup/mirror-role", response_model=ServerOut)
async def backup_mirror_role(server_id: int, body: BackupMirrorRoleIn, user: AdminUser, session: SessionDep) -> ServerOut:
    """Сделать сервер резервным: панель попросит поставить на него helper mirror-setup (в "Требует
    действий", плейбук kervax_helpers). Снять роль можно, только когда на него ничего не копируется."""
    s = await _get_or_404(server_id, session, user)
    if not body.on:
        servers = list(await session.scalars(select(Server)))
        if bm.plan(servers).get(s.id):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "на этот сервер копируются репозитории: сначала снимите копирование у них")
    s.backup_mirror_role = body.on
    await session.commit()
    await session.refresh(s)
    await audit.record(session, user.username, "backup_mirror_role_on" if body.on else "backup_mirror_role_off",
                       s.name[:120], f"srv={server_id}")
    return _out(s, datetime.now(timezone.utc))


@router.post("/{server_id}/backup/mirror-sync", response_model=ServerOut)
async def backup_mirror_sync(server_id: int, request: Request, user: AdminUser, session: SessionDep) -> ServerOut:
    """Отправить настройки копий заново: резервному и всем основным, с которых он копирует, или
    основному и его резервным. Для случая, когда настройка не дошла (агент был старый, helper
    переставили, у резервного сменился ключ)."""
    s = await _get_or_404(server_id, session, user)
    servers = list(await session.scalars(select(Server)))
    want = bm.plan(servers)
    prim = set(want.get(s.id) or {})
    mirrors = {s.id} if bm.is_mirror(s) else set()
    if bm.entries(s) or bm.reported_mirror_ids(s):
        prim.add(s.id)
        mirrors |= {e["mirror"] for e in bm.entries(s).values()}
    if not prim and not mirrors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "у сервера нет копий на резервном")
    await audit.record(session, user.username, "backup_mirror_sync", s.name[:120], f"srv={server_id}")
    _start_sync(request, prim, mirrors, user.username)
    return _out(s, datetime.now(timezone.utc))


@router.post("/{server_id}/backup/mirror-pull", response_model=BackupCommandOut)
async def backup_mirror_pull(server_id: int, user: AdminUser, session: SessionDep) -> BackupCommandOut:
    """Забрать копии на резервном сейчас, не дожидаясь ночи."""
    s = await _get_or_404(server_id, session, user)
    if not bm.is_mirror(s) or not bm.agent_ok(s):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{s.name} не резервный или агент старше {bm.AGENT_MIN}")
    c = BackupCommand(server_id=s.id, action="mirror", mode="pull", created_by=user.username)
    session.add(c)
    await session.commit()
    await session.refresh(c)
    await audit.record(session, user.username, "backup_mirror_pull", s.name[:120], f"srv={server_id}")
    return BackupCommandOut.model_validate(c)


@agent_router.post("/mirror-wanted", response_class=PlainTextResponse)
async def agent_mirror_wanted(request: Request, session: SessionDep) -> str:
    """Условие установки helper'а mirror-setup (KERVAX_SETUP_WHEN): сделан ли этот сервер резервным.
    Токен в теле, как у gre-wanted."""
    token = (await request.body())[:512].decode("ascii", errors="ignore").strip()
    server = await _by_token(session, token)
    return "yes" if server.backup_mirror_role else "no"
