"""Доступы: кто может войти на сервер и пользуется ли этим.

Данные - helper changes-setup 0.5: пользователи с shell (uid, пароль задан/заблокирован/пустой -
без хэша), последний вход по SSH, ключи с длиной, кому sudo без пароля, и "seen_since" - с какого
момента helper видит входы.

Последний вход: lastlog видит только сессии с терминалом, а ansible, CI и scp ходят без него -
их учетки выглядели бы заброшенными. Поэтому helper ведет свой учет по журналу sshd (строки
Accepted пишутся для любого входа) и берет большее из двух. Сказать "не входили 90 дней" можно,
только если смотрели 90 дней: журнал (или учет helper'а) должен покрывать этот срок. Есть одно
послабление: если журнал покрывает хотя бы 2 недели, входов за них нет, а по терминалу последний
раз входили больше года назад (или никогда), учетка тоже заброшена - так находятся учетки
ушедших людей 2020-2021 годов с паролем и sudo без пароля.

Что делаем:
- вторая учетка с uid 0 или учетка с shell и пустым паролем - проблема, алерт access_risk;
- заброшенная учетка с доступом (есть ключи или пароль при включенном входе по паролю) и слабые
  ключи (DSA, RSA короче 2048 бит) - пункт в "Требует действий".
Точечный мут - "access:<пользователь>", весь пункт - "access_audit".
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import changes as srv_changes
from app import ssh_audit
from app.models import Server

MUTE_PREFIX = "access:"
MUTE_ALL = "access_audit"
STALE = timedelta(days=90)
SHORT_LOOK = timedelta(days=14)
OLD_TERMINAL = timedelta(days=365)


def _weak(k: dict) -> bool:
    t, b = str(k.get("t") or "").upper(), int(k.get("b") or 0)
    return t == "DSA" or (t == "RSA" and 0 < b < 2048)


def _nopasswd(snap: dict) -> set[str]:
    """Пользователи с sudo без пароля: прямые правила и группы sudo/wheel/admin из правил %группа."""
    sudo = snap.get("sudo") or {}
    groups = sudo.get("groups") or {}
    out = set()
    for who in sudo.get("nopasswd") or []:
        who = str(who)
        if who.startswith("%"):
            out |= {str(m) for m in groups.get(who[1:]) or []}
        else:
            out.add(who)
    return out


def accounts(srv: Server, now: datetime) -> tuple[list[dict], int] | None:
    """Учетки с shell: [{"name", "uid", "pw", "last", "keys", "weak", "nopasswd", "access", "stale"}]
    и seen_since; None - helper старше 0.5."""
    snap = srv_changes.snapshot(srv.last_report or {})
    if not snap or "seen_since" not in snap:
        return None
    since = int(snap.get("seen_since") or 0)
    ssh = ssh_audit.block(srv.last_report or {})
    pass_login = bool(ssh and ssh_audit.password_login(ssh))
    keys: dict[str, list[dict]] = {}
    for k in snap.get("keys") or []:
        if isinstance(k, dict) and k.get("u"):
            keys.setdefault(str(k["u"]), []).append(k)
    nopw = _nopasswd(snap)
    ts = now.timestamp()
    look = ts - since if since else 0
    out = []
    for u in snap.get("users") or []:
        if not isinstance(u, dict) or not u.get("n"):
            continue
        name = str(u["n"])
        ks = keys.get(name, [])
        pw = str(u.get("pw") or "")
        last = int(u.get("last") or 0)
        access = bool(ks) or (pw == "P" and pass_login)
        unused = last < ts - STALE.total_seconds() and (
            look >= STALE.total_seconds()
            or (look >= SHORT_LOOK.total_seconds() and last < ts - OLD_TERMINAL.total_seconds()))
        out.append({"name": name, "uid": int(u.get("uid") or 0), "pw": pw, "last": last, "keys": len(ks),
                    "weak": [f"{k.get('t')}-{k.get('b')}{' ' + str(k['c']) if k.get('c') else ''}" for k in ks if _weak(k)],
                    "nopasswd": name in nopw, "access": access, "stale": access and unused})
    return out, since


def problems(srv: Server) -> list[tuple[int, str]]:
    """(уровень, текст) для алерта access_risk: второй root, пустой пароль."""
    snap = srv_changes.snapshot(srv.last_report or {})
    if not snap:
        return []
    out = []
    for u in snap.get("users") or []:
        if not isinstance(u, dict) or not u.get("n"):
            continue
        name = str(u["n"])
        if str(u.get("uid")) == "0" and name != "root":
            out.append((2, f"учетка {name} с uid 0 - это второй root"))
        if u.get("pw") == "NP":
            out.append((2, f"у учетки {name} пустой пароль: войти можно без пароля"))
    return out


def advice(srv: Server, now: datetime) -> dict | None:
    """Для "Требует действий": имена заброшенных учеток с доступом и учеток со слабыми ключами,
    без заглушенных. Подробности (что за доступ, когда входили) - в view для карточки."""
    mutes = set(srv.alert_mutes or [])
    if MUTE_ALL in mutes:
        return None
    acc = accounts(srv, now)
    if acc is None:
        return None
    rows = [a for a in acc[0] if MUTE_PREFIX + a["name"] not in mutes]
    stale = [a["name"] for a in rows if a["stale"]]
    weak = [a["name"] for a in rows if a["weak"]]
    return {"stale": stale, "weak": weak} if stale or weak else None


def view(srv: Server, now: datetime) -> dict | None:
    """Для карточки: учетки, с какого момента видны входы, заглушенные."""
    acc = accounts(srv, now)
    if acc is None:
        return None
    mutes = set(srv.alert_mutes or [])
    return {"accounts": [{**a, "muted": MUTE_PREFIX + a["name"] in mutes} for a in acc[0]], "seen_since": acc[1]}
