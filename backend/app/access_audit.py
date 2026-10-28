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

uid 0 пускает PermitRootLogin: no - никак, prohibit-password - только по ключу. Без этого root с
паролем на ноде с PermitRootLogin no считался доступной заброшенной учеткой.

У заброшенной учетки - шаги "как отключить" (fix): после них панель доступа не видит (пароль
заблокирован, файлы ключей переименованы), и пункт уходит сам. Срок учетки (usermod -e 1) helper
не передает, он для надежности: SSH не пустит и по ключу из другого места. root срок не ставят:
истекшая root ломает ее cron и su (PAM account), только passwd -l.
"""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timedelta

from app import changes as srv_changes
from app import ssh_audit, tg_fmt
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
    root_key = not ssh or str(ssh.get("root") or "") != "no"
    root_pass = bool(ssh and ssh_audit.root_password(ssh))
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
        if str(u.get("uid")) == "0":
            access = (bool(ks) and root_key) or (pw == "P" and root_pass)
        else:
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


_ROOT_RE = re.compile(r"^учетка (\S+) с uid 0 ")
_EMPTY_RE = re.compile(r"^у учетки (\S+) пустой пароль")


def tg_short(items: list[tuple[int, str]]) -> tuple[str, list[str]] | None:
    """Алерт access_risk (вход - problems()): "Второй root", "uid 0 у backup, пустой пароль у test"."""
    if not items:
        return None
    roots = [m.group(1) for _l, t in items if (m := _ROOT_RE.match(t))]
    empty = [m.group(1) for _l, t in items if (m := _EMPTY_RE.match(t))]
    if roots:
        fact = f"uid 0 у {tg_fmt.names(roots)}" + (f", пустой пароль у {tg_fmt.names(empty)}" if empty else "")
        return "Второй root", [fact]
    if empty:
        return ("Пустой пароль у учетки" if len(empty) == 1 else "Пустой пароль у учеток"), [tg_fmt.names(empty)]
    return "Опасный доступ", []


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


def _key_files(snap: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k in snap.get("keys") or []:
        if isinstance(k, dict) and k.get("u") and k.get("f") and str(k["f"]) not in out.get(str(k["u"]), []):
            out.setdefault(str(k["u"]), []).append(str(k["f"]))
    return out


def disable_fix(a: dict, files: list[str], root_pass: bool, alone: bool) -> list[dict]:
    """Как отключить заброшенную учетку a (из accounts): [{"id", "arg", "cmd"?, "alt"?, "undo"?}].
    files - ее файлы ключей, root_pass - пускает ли SSH root по паролю, alone - больше входить некем.
    Отключение - один шаг acc_off*/acc_root_*: блокировка и ключи одной командой через &&, перед ним
    у обычной учетки проверка acc_check; откат (undo) и удаление (alt) фронт прячет под свертку."""
    name = a["name"]
    if alone:
        return [{"id": "acc_alone", "arg": name}]
    if not ssh_audit.NAME_OK.match(name):
        return []
    # ключи переименовываются, а не удаляются: вернуть - mv обратно
    hide = [f"sudo mv {shlex.quote(f)} {shlex.quote(f + '.disabled')}" for f in files]
    back = [f"sudo mv {shlex.quote(f + '.disabled')} {shlex.quote(f)}" for f in files]
    lock = a["pw"] == "P"
    steps = []
    if a["uid"] == 0:
        # пароль root для консоли не трогаем, если по SSH паролем root и так не пускают
        lock = lock and root_pass
        off = ([f"sudo passwd -l {name}"] if lock else []) + hide
        undo = ([f"sudo passwd -u {name}"] if lock else []) + back
        sid = "acc_root_off" if lock and files else "acc_root_lock" if lock else "acc_root_keys"
    else:
        steps.append({"id": "acc_check", "arg": name, "cmd": (
            f"ps -u {name} -o pid,lstart,args\nsudo crontab -l -u {name}\nls /var/lib/systemd/linger/\n"
            f"grep -rlxF 'User={name}' /etc/systemd/system /lib/systemd/system")})
        # -L только у заданного пароля: у заблокированного -U потом не снимется
        off = [f"sudo usermod {'-L ' if lock else ''}-e 1 {name}"] + hide
        undo = [f"sudo usermod {'-U ' if lock else ''}-e '' {name}"] + back
        sid = "acc_off" if files else "acc_off_nokeys"
    if not off:
        return steps
    # && в конце строки: shell ждет продолжения, ключи не трогаются, если блокировка не прошла
    steps.append({"id": sid, "arg": name, "cmd": " &&\n".join(off)})
    steps.append({"id": "acc_undo", "undo": True, "arg": name, "cmd": "\n".join(undo)})
    if a["uid"] != 0:
        steps.append({"id": "acc_delete", "alt": True, "arg": name, "cmd": f"sudo userdel {name}"})
    return steps


def view(srv: Server, now: datetime) -> dict | None:
    """Для карточки: учетки, с какого момента видны входы, заглушенные; у заброшенных - fix."""
    acc = accounts(srv, now)
    if acc is None:
        return None
    mutes = set(srv.alert_mutes or [])
    files = _key_files(srv_changes.snapshot(srv.last_report or {}) or {})
    ssh = ssh_audit.block(srv.last_report or {})
    root_pass = bool(ssh and ssh_audit.root_password(ssh))
    cut = now.timestamp() - STALE.total_seconds()
    rows = []
    for a in acc[0]:
        row = {**a, "muted": MUTE_PREFIX + a["name"] in mutes}
        if a["stale"] and not row["muted"]:
            # последнюю учетку, через которую входят, отключать нельзя: не войдет никто
            alone = not any(b["access"] and b["name"] != a["name"] and b["last"] >= cut for b in acc[0])
            row["fix"] = disable_fix(a, files.get(a["name"], []), root_pass, alone)
        rows.append(row)
    return {"accounts": rows, "seen_since": acc[1]}
