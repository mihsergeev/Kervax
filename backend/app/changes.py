"""Изменения на сервере: кто может войти и что запускается по расписанию.

helper changes-setup раз в 10 минут снимает отпечатки SSH-ключей всех пользователей,
пользователей с shell, состав групп sudo/wheel/admin, хэши sudoers и кронов и список
включенных сервисов и таймеров (report.d/changes.json). Панель сравнивает снимок с прошлым и
пишет ленту изменений в servers.changes.

Алертят изменения доступа: новый SSH-ключ, новый пользователь с shell (или uid 0), кто-то
добавлен в sudo, изменились sudoers. Если это сделали свои - отметка "это мы" в карточке
убирает строку с главной. Кроны и юниты меняют деплои и пакеты постоянно, они идут только в
ленту. Первый снимок - точка отсчета.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Server

log = logging.getLogger("kervax.changes")

LOG_MAX = 200
LOG_KEEP = timedelta(days=90)
HOME_HOURS = 24


def snapshot(rep: dict) -> dict | None:
    ch = ((rep or {}).get("extras") or {}).get("changes")
    if not isinstance(ch, dict) or ch.get("v") != 1 or not ch.get("ts"):
        return None
    return ch


def _keys(snap: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for k in snap.get("keys") or []:
        if isinstance(k, dict) and k.get("fp"):
            out.setdefault((str(k.get("u") or "?"), str(k["fp"])), k)  # один ключ дважды в файле - один
    return out


def _users(snap: dict) -> dict[str, dict]:
    return {str(u["n"]): u for u in snap.get("users") or [] if isinstance(u, dict) and u.get("n")}


def _key_text(k: dict) -> str:
    c = str(k.get("c") or "").strip()
    return f"{k.get('t') or '?'} {k.get('fp')}" + (f" ({c})" if c else "")


def diff(old: dict, new: dict) -> list[dict]:
    """События между двумя снимками: {"kind", "act", "text", "alert"}."""
    ev: list[dict] = []

    def add(kind: str, act: str, text: str, alert: bool) -> None:
        ev.append({"kind": kind, "act": act, "text": text, "alert": alert})

    ok, nk = _keys(old), _keys(new)
    for (u, fp), k in nk.items():
        if (u, fp) not in ok:
            add("key", "add", f"добавлен SSH-ключ пользователю {u}: {_key_text(k)}", True)
    for (u, fp), k in ok.items():
        if (u, fp) not in nk:
            add("key", "del", f"удален SSH-ключ пользователя {u}: {_key_text(k)}", False)
    ou, nu = _users(old), _users(new)
    for name, u in nu.items():
        if name not in ou:
            add("user", "add", f"новый пользователь {name} (uid {u.get('uid')}, {u.get('sh') or '?'})", True)
        elif u.get("uid") == 0 and ou[name].get("uid") != 0:
            add("user", "mod", f"пользователь {name} получил uid 0", True)
    for name in ou:
        if name not in nu:
            add("user", "del", f"пользователь {name} больше не может войти или удален", False)
    og = ((old.get("sudo") or {}).get("groups") or {})
    ng = ((new.get("sudo") or {}).get("groups") or {})
    for g, members in ng.items():
        before = set(og.get(g) or [])
        for m in sorted(set(members or []) - before):
            add("sudo", "add", f"{m} добавлен в группу {g}", True)
        for m in sorted(before - set(members or [])):
            add("sudo", "del", f"{m} убран из группы {g}", False)
    of = ((old.get("sudo") or {}).get("files") or {})
    nf = ((new.get("sudo") or {}).get("files") or {})
    for path, h in nf.items():
        if path not in of:
            add("sudo", "add", f"добавлен {path}", True)
        elif of[path] != h:
            add("sudo", "mod", f"изменен {path}", True)
    for path in of:
        if path not in nf:
            add("sudo", "del", f"удален {path}", False)
    oc, nc = old.get("cron") or {}, new.get("cron") or {}
    for path, h in nc.items():
        if path not in oc:
            add("cron", "add", f"добавлен крон {path}", False)
        elif oc[path] != h:
            add("cron", "mod", f"изменен крон {path}", False)
    for path in oc:
        if path not in nc:
            add("cron", "del", f"удален крон {path}", False)
    on, nn = set(old.get("units") or []), set(new.get("units") or [])
    for u in sorted(nn - on):
        add("unit", "add", f"включен {u}", False)
    for u in sorted(on - nn):
        add("unit", "del", f"выключен {u}", False)
    return ev


def apply_snapshot(state: dict | None, snap: dict, now: datetime) -> dict:
    """Новый servers.changes: снимок, лента с новыми событиями, отметка последнего алертного."""
    st = dict(state or {})
    prev = st.get("snap") if isinstance(st.get("snap"), dict) else None
    if prev is not None and prev.get("ts") == snap.get("ts"):
        return st
    logv = [e for e in st.get("log") or [] if isinstance(e, dict)]
    if prev is not None:
        stamp = datetime.fromtimestamp(int(snap["ts"]), timezone.utc).isoformat()
        events = [dict(e, ts=stamp) for e in diff(prev, snap)]
        if events:
            logv = (events + logv)
            if any(e["alert"] for e in events):
                st["alert_ts"] = stamp
    cutoff = (now - LOG_KEEP).isoformat()
    st["log"] = [e for e in logv if str(e.get("ts") or "") >= cutoff][:LOG_MAX]
    st["snap"] = snap
    return st


def unacked_alerts(srv: Server, now: datetime, hours: int = HOME_HOURS) -> list[dict]:
    """Изменения доступа за сутки, которые еще не отметили как свои."""
    st = srv.changes if isinstance(getattr(srv, "changes", None), dict) else {}
    since = (now - timedelta(hours=hours)).isoformat()
    ack = str(st.get("ack_ts") or "")
    return [e for e in st.get("log") or [] if isinstance(e, dict) and e.get("alert")
            and str(e.get("ts") or "") >= since and str(e.get("ts") or "") > ack]


def alert_events(srv: Server, after: str | None) -> list[dict]:
    """Алертные события новее отметки, о которой уже сказали."""
    st = srv.changes if isinstance(getattr(srv, "changes", None), dict) else {}
    return [e for e in st.get("log") or [] if isinstance(e, dict) and e.get("alert")
            and str(e.get("ts") or "") > str(after or "")]


def events_text(events: list[dict], limit: int = 4) -> str:
    items = [e["text"] for e in events[:limit]]
    if len(events) > limit:
        items.append(f"и еще {len(events) - limit}")
    return "; ".join(items)


def view(srv: Server) -> dict | None:
    st = srv.changes if isinstance(getattr(srv, "changes", None), dict) else None
    if not st:
        return None
    snap = st.get("snap") or {}
    return {"log": (st.get("log") or [])[:60], "ack_ts": st.get("ack_ts"),
            "snap_ts": snap.get("ts"), "keys": len(_keys(snap)), "users": len(_users(snap))}


_last_track: datetime | None = None
# helper снимает раз в 10 минут, а выборка тянет отчеты всех нод: чаще раза в минуту незачем
_TRACK_EVERY = timedelta(minutes=1)


async def track(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Свежие снимки helper'а - в ленту. Сравнивает только тех, у кого снимок новее сохраненного."""
    global _last_track
    if _last_track is not None and now - _last_track < _TRACK_EVERY:
        return 0
    _last_track = now
    n = 0
    async with session_factory() as s:
        for srv in await s.scalars(select(Server).where(Server.enabled.is_(True))):
            snap = snapshot(srv.last_report or {})
            if snap is None:
                continue
            cur = srv.changes if isinstance(srv.changes, dict) else {}
            if (cur.get("snap") or {}).get("ts") == snap.get("ts"):
                continue
            srv.changes = apply_snapshot(cur, snap, now)
            n += 1
        if n:
            await s.commit()
    return n
