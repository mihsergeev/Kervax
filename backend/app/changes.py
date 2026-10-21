"""Изменения на сервере: кто может войти и что запускается по расписанию.

helper changes-setup раз в 10 минут снимает отпечатки SSH-ключей всех пользователей,
пользователей с shell, состав групп sudo/wheel/admin, хэши sudoers и кронов и список
включенных сервисов и таймеров (report.d/changes.json). Панель сравнивает снимок с прошлым и
пишет ленту изменений в servers.changes.

Алертят изменения доступа: новый SSH-ключ, новый пользователь с shell (или uid 0), кто-то
добавлен в sudo, изменились sudoers. Ключ, который уже встречался на любом сервере парка (лежит в
снимке или был добавлен раньше), не алертит: это наш же деплой разносит свой ключ, событие только в
ленте и хронологии с пометкой. Алертит незнакомый ключ, которого парк еще не видел. Если это сделали свои - отметка "это мы" в карточке
убирает строку с главной. Кроны и юниты меняют деплои и пакеты постоянно, они идут только в
ленту. Первый снимок - точка отсчета.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import tg_fmt
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
            ev[-1]["fp"] = fp
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


# Добавленное и потом убранное: что именно добавили (по тексту события, в старых записях
# ленты другого нет) и как сказать, что этого уже нет
_ADD_DEL = [
    ("key", re.compile(r"^добавлен SSH-ключ пользователю (\S+): (.+)$"),
     re.compile(r"^удален SSH-ключ пользователя (\S+): (.+)$"), "позже удален"),
    ("user", re.compile(r"^новый пользователь (\S+) \("),
     re.compile(r"^пользователь (\S+) больше не может войти или удален$"), "позже удален или больше не может войти"),
    ("sudo", re.compile(r"^(\S+) добавлен в группу (\S+)$"), re.compile(r"^(\S+) убран из группы (\S+)$"), "позже убран"),
    ("sudo", re.compile(r"^добавлен (/\S+)$"), re.compile(r"^удален (/\S+)$"), "позже удален"),
]


def _subject(e: dict) -> tuple[str | None, tuple | None, str]:
    """("add"/"del", что именно, пометка) для событий, у которых бывает пара."""
    text = str(e.get("text") or "")
    for i, (kind, add_re, del_re, note) in enumerate(_ADD_DEL):
        if e.get("kind") != kind:
            continue
        for act, rx in (("add", add_re), ("del", del_re)):
            m = rx.match(text)
            if m and e.get("act") == act:
                return act, (i, *m.groups()), note
    return None, None, ""


def annotate(log: list[dict]) -> list[dict]:
    """Копия ленты (новые сверху), где добавленное и позже убранное помечено: "добавлен ключ"
    без пометки читается как висящий доступ, хотя ключ уже сняли. Строка не прячется - видно,
    что ключ кто-то на время клал."""
    removed: set[tuple] = set()
    out = []
    for e in log:
        act, subj, note = _subject(e) if isinstance(e, dict) else (None, None, "")
        if act == "del":
            removed.add(subj)
        elif act == "add" and subj in removed:
            removed.discard(subj)  # еще раньше могли добавить и снять снова - то отдельная пара
            e = dict(e, text=f"{e['text']} ({note})")
        if isinstance(e, dict) and e.get("known"):
            e = dict(e, text=e["text"] + KNOWN_NOTE)
        out.append(e)
    return out


SSH_SEEN_KEEP = timedelta(days=90)
_PASSWORD_METHODS = ("password", "keyboard-interactive", "keyboard-interactive/pam")


def ssh_login_events(state: dict, snap: dict, stamp: str) -> list[dict]:
    """Входы по SSH из снимка (changes-setup 0.4). Вход по паролю с адреса, с которого этот
    пользователь раньше не входил, - событие с алертом: так выглядит подобранный пароль. Входы
    по ключу запоминаем молча. Первый снимок с входами - точка отсчета, без событий.
    state["ssh_seen"] обновляется на месте: {"пользователь|адрес": когда видели}, state["ssh_pw"] -
    {"пользователь": последний вход по паролю}: кто не войдет, если выключить пароли (ssh_audit)."""
    baseline = "ssh_seen" not in state
    seen = dict(state.get("ssh_seen") or {})
    pw = dict(state.get("ssh_pw") or {})
    ev = []
    for lg in ((snap.get("ssh") or {}).get("logins") or []):
        if not isinstance(lg, dict) or not lg.get("u") or not lg.get("ip"):
            continue
        key = f"{lg['u']}|{lg['ip']}"
        by_pw = str(lg.get("m") or "") in _PASSWORD_METHODS
        if key not in seen and not baseline and by_pw:
            ev.append({"kind": "login", "act": "add", "alert": True,
                       "text": f"вход по паролю: {lg['u']} с {lg['ip']}, раньше с этого адреса не входил"})
        seen[key] = stamp
        if by_pw:
            pw[str(lg["u"])] = stamp
    cutoff = (datetime.fromisoformat(stamp) - SSH_SEEN_KEEP).isoformat()
    state["ssh_seen"] = dict(sorted(((k, v) for k, v in seen.items() if v >= cutoff), key=lambda kv: kv[1])[-500:])
    state["ssh_pw"] = dict(sorted(((k, v) for k, v in pw.items() if v >= cutoff), key=lambda kv: kv[1])[-200:])
    return ev


_ADD_KEY = re.compile(r"^добавлен SSH-ключ пользователю \S+: \S+ (\S+)")
KNOWN_NOTE = " (ключ уже был на серверах парка)"


def event_fp(e: dict) -> str:
    """Отпечаток добавленного ключа из события ленты: в старых записях только в тексте"""
    if e.get("kind") != "key" or e.get("act") != "add":
        return ""
    if e.get("fp"):
        return str(e["fp"])
    m = _ADD_KEY.match(str(e.get("text") or ""))
    return m.group(1) if m else ""


def known_fps(states) -> set[str]:
    """Ключи, которые парк уже видел: лежат в снимке любого сервера или были добавлены по ленте
    (даже если потом их сняли). states - servers.changes всех серверов"""
    out: set[str] = set()
    for st in states:
        if not isinstance(st, dict):
            continue
        snap = st.get("snap") if isinstance(st.get("snap"), dict) else {}
        out.update(fp for _u, fp in _keys(snap))
        out.update(fp for e in st.get("log") or [] if isinstance(e, dict) and (fp := event_fp(e)))
    return out


def text_of(e: dict) -> str:
    """Текст события для ленты и хронологии: знакомый ключ с пометкой"""
    return str(e.get("text") or "") + (KNOWN_NOTE if e.get("known") else "")


def apply_snapshot(state: dict | None, snap: dict, now: datetime, known: set[str] | None = None) -> dict:
    """Новый servers.changes: снимок, лента с новыми событиями, отметка последнего алертного.
    known - ключи, которые парк уже видел (known_fps): их добавление в ленту без алерта."""
    st = dict(state or {})
    prev = st.get("snap") if isinstance(st.get("snap"), dict) else None
    if prev is not None and prev.get("ts") == snap.get("ts"):
        return st
    logv = [e for e in st.get("log") or [] if isinstance(e, dict)]
    stamp = datetime.fromtimestamp(int(snap["ts"]), timezone.utc).isoformat()
    logins = ssh_login_events(st, snap, stamp) if (snap.get("ssh") or {}).get("logins") is not None else []
    if prev is not None or logins:
        events = [dict(e, ts=stamp) for e in (diff(prev, snap) if prev is not None else []) + logins]
        for e in events:
            if known and e.get("alert") and event_fp(e) in known:
                e["alert"], e["known"] = False, True
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
    return [e for e in annotate(st.get("log") or []) if isinstance(e, dict) and e.get("alert")
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


# ---- короткий текст алерта в Telegram (app/tg_fmt.py) ----

# разбор текстов событий из diff и ssh_login_events (в старых записях ленты другого нет)
_EV_RE = [
    ("key", re.compile(r"^добавлен SSH-ключ пользователю (?P<u>\S+): \S+ \S+(?: \((?P<c>.*)\))?$")),
    ("user", re.compile(r"^новый пользователь (?P<u>\S+) \(uid (?P<uid>[^,]*),")),
    ("uid0", re.compile(r"^пользователь (?P<u>\S+) получил uid 0$")),
    ("group", re.compile(r"^(?P<u>\S+) добавлен в группу (?P<g>\S+)$")),
    ("file", re.compile(r"^(?:добавлен|изменен) (?P<path>/\S+)$")),
    ("login", re.compile(r"^вход по паролю: (?P<u>\S+) с (?P<ip>\S+),")),
]
_COMMENT_MAX = 40


def _parse(e: dict) -> dict:
    """{"what": вид, "u", "c", "g", "path", "ip"} по тексту события; вид "" - не разобрали."""
    text = str(e.get("text") or "")
    for what, rx in _EV_RE:
        m = rx.match(text)
        if m:
            d = {k: v for k, v in m.groupdict().items() if v}
            if what == "user" and str(d.get("uid")) == "0":
                what = "uid0"
            return {"what": what, **d}
    return {"what": ""}


def _comment(c: str) -> str:
    c = str(c or "").strip()
    return c if len(c) <= _COMMENT_MAX else c[:_COMMENT_MAX - 3].rstrip() + "..."


def _sudo_of(p: dict) -> str:
    """Кому событие дает sudo: добавлен в группу или файл /etc/sudoers.d/<имя>."""
    if p["what"] == "group":
        return p.get("u", "")
    if p["what"] == "file" and p.get("path", "").startswith("/etc/sudoers.d/"):
        return p["path"].rsplit("/", 1)[-1]
    return ""


def _more(p: dict) -> str:
    """Событие, не вошедшее в главное, одним-двумя словами."""
    u = p.get("u", "")
    return {"key": f"ключ {u}", "user": f"пользователь {u}", "uid0": f"uid 0 у {u}", "group": f"sudo у {u}",
            "file": p.get("path", ""), "login": f"вход {u} по паролю"}.get(p["what"], "")


def tg_short(events: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт access_change (вход - alert_events()): главное событие одной фразой, конкретика - кто и
    комментарий ключа, без отпечатков. Главное по опасности: uid 0, вход по паролю с нового адреса,
    новый пользователь с sudo, sudo старому пользователю, новый пользователь, ключ, файл sudoers.
    События про того же пользователя входят в главное, остальные - строкой "Еще: ..."."""
    if not events:
        return None
    ps = [_parse(e) for e in events if isinstance(e, dict)]
    users = {p["u"] for p in ps if p["what"] in ("user", "uid0") and p.get("u")}
    # файл в sudoers.d зовут по пользователю не всегда (90-cloud-init-users): по имени файла sudo
    # считаем только у нового пользователя с тем же именем
    group_sudo = [p["u"] for p in ps if p["what"] == "group" and p.get("u")]
    sudo = set(group_sudo) | {_sudo_of(p) for p in ps if p["what"] == "file" and _sudo_of(p) in users}

    def keys_of(u: str) -> list[str]:
        return [_comment(p.get("c", "")) for p in ps if p["what"] == "key" and p.get("u") == u]

    def user_fact(names: list[str]) -> str:
        if len(names) != 1:
            return tg_fmt.names(names)
        ks = keys_of(names[0])
        cs = [c for c in ks if c]
        return names[0] + (f", вход по ключу {tg_fmt.names(cs)}" if cs else ", вход по ключу" if ks else "")

    uid0 = [p["u"] for p in ps if p["what"] == "uid0" and p.get("u")]
    logins = [p for p in ps if p["what"] == "login"]
    new_sudo = [u for u in dict.fromkeys(p.get("u") for p in ps if p["what"] == "user") if u in sudo]
    new_users = [u for u in dict.fromkeys(p.get("u") for p in ps if p["what"] == "user") if u]
    old_sudo = [u for u in dict.fromkeys(group_sudo) if u not in users]
    keys = [p for p in ps if p["what"] == "key"]
    files = list(dict.fromkeys(p["path"] for p in ps if p["what"] == "file" and _sudo_of(p) not in users))

    # главное: (что, конкретика, о ком оно - их события в "Еще" не повторяем)
    if uid0:
        uniq = list(dict.fromkeys(uid0))
        main = ("Пользователь получил uid 0" if len(uniq) == 1 else "Пользователи получили uid 0",
                tg_fmt.names(uniq), set(uniq), {"uid0", "user"})
    elif logins:
        items = list(dict.fromkeys(f"{p.get('u')} с {p.get('ip')}" for p in logins))
        main = ("Вход по паролю с нового адреса", tg_fmt.names(items), set(), {"login"})
    elif new_sudo:
        main = ("Новый пользователь с sudo" if len(new_sudo) == 1 else "Новые пользователи с sudo",
                user_fact(new_sudo), set(new_sudo), {"user", "key", "group", "file"})
    elif old_sudo:
        main = ("Выданы права sudo", tg_fmt.names(old_sudo), set(old_sudo), {"group", "file"})
    elif new_users:
        main = ("Новый пользователь" if len(new_users) == 1 else "Новые пользователи",
                user_fact(new_users), set(new_users), {"user", "key"})
    elif keys:
        by: dict[str, list[str]] = {}
        for p in keys:
            by.setdefault(p.get("u", "?"), []).append(_comment(p.get("c", "")))
        if len(by) == 1:
            u, cs = next(iter(by.items()))
            cs = [c for c in cs if c]
            fact = f"{u}: {tg_fmt.names(cs)}" if cs else u
        else:
            fact = tg_fmt.names(f"{u}: {', '.join(c for c in cs if c)}" if any(cs) else u for u, cs in by.items())
        main = ("Добавлен SSH-ключ" if len(keys) == 1 else "Добавлены SSH-ключи", fact, set(by), {"key"})
    elif files:
        main = ("Изменились права sudo", tg_fmt.names(files), set(), {"file"})
    else:
        return "Изменился доступ", []
    what, fact, who, kinds = main

    def covered(p: dict) -> bool:
        if p["what"] in kinds and (p.get("u") in who or _sudo_of(p) in who):
            return True
        # само главное без "о ком": все входы, все файлы sudoers
        return not who and p["what"] in kinds

    rest = [_more(p) for p in ps if p["what"] and not covered(p)]
    facts = [fact] if fact else []
    if any(rest):
        facts.append("Еще: " + tg_fmt.names(x for x in rest if x))
    return what, facts


def view(srv: Server) -> dict | None:
    st = srv.changes if isinstance(getattr(srv, "changes", None), dict) else None
    if not st:
        return None
    snap = st.get("snap") or {}
    return {"log": annotate(st.get("log") or [])[:60], "ack_ts": st.get("ack_ts"),
            "snap_ts": snap.get("ts"), "keys": len(_keys(snap)), "users": len(_users(snap))}


_last_track: datetime | None = None
# helper снимает раз в 10 минут, а выборка тянет отчеты всех нод: чаще раза в минуту незачем
_TRACK_EVERY = timedelta(minutes=1)


async def track(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Свежие снимки helper'а - в ленту. Сравнивает только тех, у кого снимок новее сохраненного.
    Ключи, которые парк уже видел, собираются по всем серверам (и выключенным) до разбора; ключ из
    только что разобранного снимка для следующих серверов уже знакомый."""
    global _last_track
    if _last_track is not None and now - _last_track < _TRACK_EVERY:
        return 0
    _last_track = now
    n = 0
    async with session_factory() as s:
        servers = list(await s.scalars(select(Server)))
        known = known_fps(srv.changes for srv in servers)
        for srv in servers:
            if not srv.enabled:
                continue
            snap = snapshot(srv.last_report or {})
            if snap is None:
                continue
            cur = srv.changes if isinstance(srv.changes, dict) else {}
            if (cur.get("snap") or {}).get("ts") == snap.get("ts"):
                continue
            srv.changes = apply_snapshot(cur, snap, now, known)
            known.update(fp for _u, fp in _keys(snap))
            n += 1
        if n:
            await s.commit()
    return n
