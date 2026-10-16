"""Защита входа (окно Пользователи, раздел "Защита входа", только админ): правило блокировки,
белый список адресов, кто заблокирован сейчас и с каких адресов были неудачные входы за сутки.
Сама блокировка - app/ratelimit.py и вход в api/auth.py."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import audit, ratelimit
from app.api.auth import _client_key
from app.deps import AdminUser, SessionDep
from app.models import AuditLog, LoginBan

router = APIRouter(prefix="/login-guard", tags=["login-guard"])

_FAILS = ("login_fail", "login_2fa_fail")
_RECENT_ROWS = 5000
_RECENT_SHOWN = 50


class AllowEntry(BaseModel):
    net: str = Field(max_length=64)
    note: str = Field(default="", max_length=80)


class GuardIn(BaseModel):
    max_failures: int
    ban_minutes: int
    allow: list[AllowEntry] = Field(default_factory=list)


class UnbanIn(BaseModel):
    key: str = Field(max_length=64)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def _out(session, request: Request) -> dict:
    g = await ratelimit.load(session)
    now = datetime.now(timezone.utc)
    me = _client_key(request)
    bans = list(await session.scalars(
        select(LoginBan).where(LoginBan.until > now).order_by(LoginBan.until.desc())))
    banned = {b.key: b for b in bans}
    rows = (await session.execute(
        select(AuditLog.target, AuditLog.username, AuditLog.ts)
        .where(AuditLog.action.in_(_FAILS), AuditLog.ts >= now - timedelta(days=1))
        .order_by(AuditLog.id.desc()).limit(_RECENT_ROWS))).all()
    by_key: dict[str, dict] = {}
    for key, name, ts in rows:
        r = by_key.setdefault(key or "", {"key": key or "", "fails": 0, "logins": [], "last": _aware(ts)})
        r["fails"] += 1
        if name and name not in r["logins"]:
            r["logins"].append(name)
    recent = sorted(by_key.values(), key=lambda r: (-r["fails"], r["key"]))[:_RECENT_SHOWN]
    for r in recent:
        a = g.allowed(r["key"])
        b = banned.get(r["key"])
        r["last"] = r["last"].isoformat()
        r["allowed"] = (a.get("note") or a["net"]) if a else None
        r["banned_until"] = _aware(b.until).isoformat() if b else None
    mine = g.allowed(me)
    return {
        "max_failures": g.max_failures,
        "ban_minutes": g.ban_minutes,
        "window_minutes": ratelimit.WINDOW // 60,
        "fail_choices": list(ratelimit.FAIL_CHOICES),
        "ban_choices": list(ratelimit.BAN_CHOICES),
        "allow": g.allow,
        "my_ip": me,
        "my_allowed": (mine.get("note") or mine["net"]) if mine else None,
        "bans": [
            {
                "key": b.key,
                "since": _aware(b.created_at).isoformat(),
                "until": _aware(b.until).isoformat(),
                "failures": b.failures,
                "logins": b.logins,
                "repeat": b.repeat,
                "blocked": b.blocked,
                "allowed": bool(g.allowed(b.key)),
            }
            for b in bans
        ],
        "recent": recent,
    }


@router.get("")
async def get_guard(_: AdminUser, session: SessionDep, request: Request) -> dict:
    return await _out(session, request)


@router.put("")
async def put_guard(body: GuardIn, admin: AdminUser, session: SessionDep, request: Request) -> dict:
    if body.max_failures not in ratelimit.FAIL_CHOICES or body.ban_minutes not in ratelimit.BAN_CHOICES:
        raise HTTPException(422, "Недопустимое правило блокировки")
    if len(body.allow) > ratelimit.ALLOW_MAX:
        raise HTTPException(422,
                            f"В белом списке не больше {ratelimit.ALLOW_MAX} записей")
    allow: list[dict] = []
    seen: set[str] = set()
    for a in body.allow:
        try:
            net = ratelimit.parse_net(a.net)
        except ValueError:
            raise HTTPException(422,
                                f"Не адрес и не подсеть: {a.net.strip()[:64]}") from None
        if net in seen:
            continue
        seen.add(net)
        allow.append({"net": net, "note": a.note.strip()})
    old = await ratelimit.load(session)
    await ratelimit.save(session, ratelimit.Guard(body.max_failures, body.ban_minutes, allow))
    nets = lambda g: {x["net"] for x in g}  # noqa: E731
    added, removed = sorted(nets(allow) - nets(old.allow)), sorted(nets(old.allow) - nets(allow))
    detail = f"{body.max_failures} неудач, блокировка {body.ban_minutes} мин"
    if added:
        detail += "; в белый список: " + ", ".join(added)
    if removed:
        detail += "; из белого списка: " + ", ".join(removed)
    await audit.record(session, admin.username, "login_guard", "", detail[:1000])
    return await _out(session, request)


@router.post("/unban")
async def unban(body: UnbanIn, admin: AdminUser, session: SessionDep, request: Request) -> dict:
    if await ratelimit.unban(session, body.key):
        await audit.record(session, admin.username, "login_unban", body.key)
    return await _out(session, request)
