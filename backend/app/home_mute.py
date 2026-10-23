"""Заглушка до нового для групп "Требует действий" на главной (кроме ОС - у нее osupd:<вид>@, os_support).

Пункты "Требует действий" - не алерты, а дела: старый образ прокси, базы без дампа, учетки, в которые
давно не входят. Часть из них человек сейчас делать не будет, и с главной их глушат галками по многим
серверам сразу. Заглушка - ключ alert_mutes "home:<группа>@<отпечаток>": отпечаток - то, что пункт
говорит про этот сервер (какие контейнеры, базы, учетки, версия). Появилось новое (еще одна база,
другой контейнер, другая версия) - отпечаток другой, пункт возвращается сам. Ушел пункт (поправили) -
заглушка больше ни о чем. В обоих случаях сборщик ее убирает с записью mute_lifted в журнал (stale).

Где у пункта нет состояния, кроме "есть" (Docker без доступа, бэкап-сервер без статистики, идет
подключение), ключ без отпечатка: "home:<группа>". Он держится, пока пункт есть, и снимается, когда
пункт ушел, - вернется, если сломается снова. Вернуть раньше - руками, на главной или в карточке.

Группы и их ключи совпадают с фронтом (HomePage.tsx actionItems): там строка группы показывает
пункт заглушенным, если его ключ отсюда (home_mutes в ServerOut) есть в alert_mutes сервера.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Callable

from app import access_audit, docker_exposure, docker_risk, onboard, reboot_survive, ssh_audit
from app import backup_target, disk_wear, kube_coverage, software as software_mod
from app.models import Server

PREFIX = "home:"
# износ дисков: в "Требует действий" то, что дойдет до 90% за полгода (фронт WEAR_PLAN_MS)
WEAR_PLAN = timedelta(days=183)

# что за пункт, для записи в журнал о снятой заглушке
LABELS = {
    "d-acc": "Docker без доступа",
    "k-acc": "Kubernetes без доступа",
    "d-old": "устарел образ внешнего прокси",
    "k-noagent": "ноды кластера без агента",
    "wear-plan": "износ дисков",
    "bs-restic": "старый restic на сервере бэкапов",
    "onb": "идет подключение",
    "bs-setup": "бэкап-сервер без статистики",
    "dbdump": "базы без отдельного дампа",
    "bk-http": "бэкап по http",
    "sw-eol": "ПО без поддержки",
    "sw-soon": "скоро кончится поддержка ПО",
    "dockrisk": "ключи от хоста у контейнеров",
    "noboot-d": "контейнеры без restart policy",
    "noboot-u": "юниты не включены",
    "access-s": "давно не входят в учетки",
    "access-w": "слабые SSH-ключи",
    "ssh-ssh_password": "SSH пускает по паролю",
}
# без отпечатка: у пункта нет состояния, кроме "есть"
PLAIN = {"d-acc", "k-acc", "onb", "bs-setup"}

# Отпечаток группы: None - данных нет (helper не прислал блок, старый агент), такую заглушку не
# трогаем; "" - пункта нет; иначе - строка, из которой хеш отпечатка
Sig = Callable[[Server, datetime, dict], "str | None"]


def _h(parts) -> str:
    return ",".join(sorted({str(p) for p in parts if p}))


def _docker(rep: dict) -> dict | None:
    d = rep.get("docker")
    return d if isinstance(d, dict) else None


def _kube(rep: dict) -> dict | None:
    k = rep.get("kube")
    return k if isinstance(k, dict) else None


def _d_acc(s: Server, now: datetime, ctx: dict) -> str | None:
    d = _docker(s.last_report or {})
    if d is None:
        return None
    return "1" if d.get("present") and not d.get("access") else ""


def _k_acc(s: Server, now: datetime, ctx: dict) -> str | None:
    k = _kube(s.last_report or {})
    if k is None:
        return None
    return "1" if k.get("present") and not k.get("access") else ""


def _d_old(s: Server, now: datetime, ctx: dict) -> str | None:
    rep = s.last_report or {}
    if _docker(rep) is None:
        return None
    return _h(f"{p['name']}={p.get('image')}" for p in docker_exposure.old_view(rep, now.timestamp(), s.alert_mutes)
              if not p["muted"])


def _k_noagent(s: Server, now: datetime, ctx: dict) -> str | None:
    idx = ctx.get("kube_index")
    k = _kube(s.last_report or {})
    if idx is None or k is None:
        return None
    return _h(n["name"] for n in kube_coverage.unmonitored(s, idx))


def _wear(s: Server, now: datetime, ctx: dict) -> str | None:
    from app.collector import disk_health_block
    block = disk_health_block(s.last_report or {})
    if block is None:
        return None
    out = []
    for w in disk_wear.card(block, s.disk_wear):
        try:
            d90 = datetime.fromisoformat(str(w.get("d90") or "")).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        if not w.get("soon") and d90 - now < WEAR_PLAN:
            out.append(w.get("serial") or w.get("dev"))
    return _h(out)


def _bs_restic(s: Server, now: datetime, ctx: dict) -> str | None:
    from app.collector import bsrv_restic_old
    rep = s.last_report or {}
    if not isinstance(rep.get("backup_server"), dict):
        return None
    return bsrv_restic_old(rep)


def _onb(s: Server, now: datetime, ctx: dict) -> str | None:
    return "1" if onboard.active(s, now) else ""


def _bs_setup(s: Server, now: datetime, ctx: dict) -> str | None:
    bs = (s.last_report or {}).get("backup_server")
    if not isinstance(bs, dict):
        return None
    return "1" if bs.get("present") and not bs.get("repos") else ""


def _dbdump(s: Server, now: datetime, ctx: dict) -> str | None:
    if s.db_dumps_ok:
        return ""
    from app.api.servers import _backup_coverage
    return _h(f"{x.subject}|{x.instance or x.container or ''}" for x in _backup_coverage(s)
              if x.kind == "db" and not x.muted)


def _bk_http(s: Server, now: datetime, ctx: dict) -> str | None:
    t = backup_target.view(s.last_report or {}, s.alert_mutes)
    if t is None:
        return None
    return f"{t.get('host')}:{t.get('port')}" if t["insecure"] and not t["muted"] else ""


def _sw(level: int) -> Sig:
    def sig(s: Server, now: datetime, ctx: dict) -> str | None:
        adv = software_mod.advice(s.last_report or {}, s.sw_host, now, s.alert_mutes)
        return _h(x["what"] for x in adv if (2 if x["level"] >= 2 else 1) == level)
    return sig


def _dockrisk(s: Server, now: datetime, ctx: dict) -> str | None:
    if _docker(s.last_report or {}) is None:
        return None
    if "docker_risk" in (s.alert_mutes or []):
        return ""
    return _h(f"{x['name']}:{'+'.join(map(str, x.get('flags') or []))}:{'+'.join(map(str, x.get('caps') or []))}"
              for x in docker_risk.view(s) if not x.get("muted"))


def _noboot(part: str) -> Sig:
    def sig(s: Server, now: datetime, ctx: dict) -> str | None:
        rep = s.last_report or {}
        if part == "docker" and _docker(rep) is None:
            return None
        if part == "units" and not isinstance((rep.get("extras") or {}).get("units"), dict):
            return None
        return _h((reboot_survive.advice(s, now) or {}).get(part) or [])
    return sig


def _access(part: str) -> Sig:
    def sig(s: Server, now: datetime, ctx: dict) -> str | None:
        if access_audit.accounts(s, now) is None:
            return None
        return _h((access_audit.advice(s, now) or {}).get(part) or [])
    return sig


def _ssh(s: Server, now: datetime, ctx: dict) -> str | None:
    if ssh_audit.block(s.last_report or {}) is None:
        return None
    # уровень: вход по паролю стал пускать и root - это новое
    return _h(f"{a['kind']}:{a['level']}" for a in ssh_audit.advice(s, now) if not a["muted"])


SIGS: dict[str, Sig] = {
    "d-acc": _d_acc, "k-acc": _k_acc, "d-old": _d_old, "k-noagent": _k_noagent, "wear-plan": _wear,
    "bs-restic": _bs_restic, "onb": _onb, "bs-setup": _bs_setup, "dbdump": _dbdump, "bk-http": _bk_http,
    "sw-eol": _sw(2), "sw-soon": _sw(1), "dockrisk": _dockrisk, "noboot-d": _noboot("docker"),
    "noboot-u": _noboot("units"), "access-s": _access("stale"), "access-w": _access("weak"),
    "ssh-ssh_password": _ssh,
}


def key(grp: str, sig: str) -> str:
    if grp in PLAIN:
        return PREFIX + grp
    return f"{PREFIX}{grp}@{hashlib.sha1(sig.encode()).hexdigest()[:10]}"


def _sig(grp: str, s: Server, now: datetime, ctx: dict) -> str | None:
    try:
        return SIGS[grp](s, now, ctx)
    except Exception:  # noqa: BLE001 - кривой блок helper'а не должен ронять ни список серверов, ни тик
        return None


def keys(s: Server, now: datetime, kube_index: tuple[set[str], set[str]] | None = None) -> dict[str, str]:
    """Ключи заглушки для пунктов, которые сейчас есть на сервере: {группа: ключ}."""
    ctx = {"kube_index": kube_index}
    out = {}
    for grp in SIGS:
        sig = _sig(grp, s, now, ctx)
        if sig:
            out[grp] = key(grp, sig)
    return out


def stale(s: Server, now: datetime, kube_index: tuple[set[str], set[str]] | None = None) -> list[tuple[str, str]]:
    """Заглушки, которые уже не про то, что на сервере: [(ключ, почему снята)]. Без данных (None) не
    трогаем: helper мог не прислать блок, а заглушка про то, что он покажет снова."""
    ctx = {"kube_index": kube_index}
    out = []
    for m in s.alert_mutes or []:
        m = str(m)
        if not m.startswith(PREFIX):
            continue
        grp = m[len(PREFIX):].split("@", 1)[0]
        if grp not in SIGS:
            continue
        sig = _sig(grp, s, now, ctx)
        if sig is None:
            continue
        label = LABELS.get(grp, grp)
        if not sig:
            out.append((m, f"заглушка снята, {label}: пункт ушел"))
        elif key(grp, sig) != m:
            out.append((m, f"заглушка снята, {label}: появилось новое"))
    return out
