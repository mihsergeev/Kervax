"""Резервный сервер бэкапов: вторая копия выбранных restic-репозиториев основных серверов бэкапов.

Как устроено (подробно - agent/mirror-setup.sh и docs/security.md):
- резервный сам забирает копии по SSH учеткой только для чтения на основном (backupserver-setup 0.38,
  mirror-allow). У основного нет ни ключа, ни входа на резервный;
- панель только называет, что копировать: основному - какой резервный и что может читать, резервному -
  откуда и что брать. Удалить копию, сменить адрес или ключ уже известного основного, разморозить копию
  панель не может: это команды root на самом резервном;
- удаление на основном не удаляет файлы из копии; большая потеря разом замораживает копирование.

Здесь: что по настройкам панели должно быть на серверах (plan), команды для них (allow_payload,
set_payload) и беды копий для алерта backup_mirror и "Что сломано". Дошла ли настройка до серверов,
фронт считает сам по их отчетам (BsrvMirror.tsx entryDrift).
"""

import re
from datetime import datetime, timedelta

from sqlalchemy import String, cast, or_, select

from app.models import BackupCommand, Server

AGENT_MIN = "2.57"        # агент знает команды mirror_allow и mirror
BSRV_HELPER_MIN = (0, 38)  # backupserver-setup: mirror-allow, ssh и mirrors в отчете
MIRROR_HELPER_MIN = (0, 2)  # source deletions never remove copied files
PULL_RETRY = timedelta(minutes=10)
# Отчет helper'а старше этого - его нет: helper не работает, а копии в нем застыли
REPORT_FRESH_S = 15 * 60
# Копия забирается раз в сутки. Отстала - если последней удачной перекачке больше двух суток: один
# пропущенный день (обрыв связи до основного) проходит сам, второй уже беда
STALE_S = 50 * 3600
# Новая копия: первая перекачка большого репозитория идет долго, но к концу суток должна закончиться
FIRST_S = 26 * 3600
VERIFY_STALE_S = 10 * 86400


def mirror_name(mirror_id: int) -> str:
    """Имя резервного на основном: по id, чтобы переименование в панели не плодило вторую учетку."""
    return f"mirror-{mirror_id}"


def src_name(name: str, sid: int) -> str:
    """Имя основного на резервном: из имени сервера при выборе, дальше не меняется."""
    s = "".join(c for c in (name or "").lower() if c.isalnum() or c in "._-").strip("._-")[:60]
    return s or f"server-{sid}"


def _vt(v: object) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:2])


def _rep(srv) -> dict:
    return getattr(srv, "last_report", None) or {}


def _fresh(block: object, rep: dict, now: datetime | None) -> dict | None:
    if not isinstance(block, dict) or block.get("v") != 1 or not isinstance(block.get("ts"), (int, float)) or not block["ts"]:
        return None
    ref = float(rep.get("clock_unix") or (now.timestamp() if now else 0) or 0)
    if ref and ref - float(block.get("ts") or 0) > REPORT_FRESH_S:
        return None
    return block


def mirror_report(srv, now: datetime | None = None) -> dict | None:
    """Блок helper'а mirror-setup резервного (report.d/backup-mirror.json), если он свежий."""
    rep = _rep(srv)
    return _fresh((rep.get("extras") or {}).get("backup-mirror"), rep, now)


def bsrv_report(srv, now: datetime | None = None) -> dict | None:
    """Блок helper'а backupserver-setup основного (report.d/backup-server.json), если он свежий."""
    rep = _rep(srv)
    return _fresh((rep.get("extras") or {}).get("backup-server"), rep, now)


def is_mirror(srv) -> bool:
    """Резервный: так отмечен в панели или на нем уже стоит helper."""
    return bool(getattr(srv, "backup_mirror_role", False)) or "backup-mirror" in ((_rep(srv).get("extras")) or {})


def agent_ok(srv) -> bool:
    return _vt(getattr(srv, "agent_version", "")) >= _vt(AGENT_MIN)


def helper_ver(srv, name: str) -> tuple[int, ...]:
    return _vt((_rep(srv).get("setup_versions") or {}).get(name))


def entries(srv) -> dict[str, dict]:
    """Репозитории основного, копия которых должна быть на резервном: имя -> {mirror, src, ...}."""
    out = {}
    for repo, e in (getattr(srv, "backup_mirror", None) or {}).items():
        if isinstance(e, dict) and isinstance(e.get("mirror"), int) and e.get("src"):
            out[repo] = e
    return out


def plan(servers) -> dict[int, dict[int, list[str]]]:
    """Что должно быть по настройкам панели: id резервного -> id основного -> репозитории."""
    out: dict[int, dict[int, list[str]]] = {}
    for p in servers:
        for repo, e in entries(p).items():
            out.setdefault(e["mirror"], {}).setdefault(p.id, []).append(repo)
    for prim in out.values():
        for k in prim:
            prim[k] = sorted(set(prim[k]))
    return out


def ssh_of(primary, now: datetime | None = None) -> dict | None:
    """Порт SSH и ключ хоста основного, которые запомнит резервный (backupserver-setup 0.38+)."""
    b = bsrv_report(primary, now) or {}
    s = b.get("ssh")
    if isinstance(s, dict) and s.get("hostkey") and isinstance(s.get("port"), int):
        return s
    return None


def pick_ip(srv) -> str:
    return (getattr(srv, "external_ip", "") or getattr(srv, "agent_ip", "") or "").strip()


def why_not_primary(p, now: datetime | None = None) -> str:
    """Почему с этого основного пока нельзя копировать (пусто - можно)."""
    if not agent_ok(p):
        return f"на {p.name} нужен агент {AGENT_MIN} или новее"
    if helper_ver(p, "backupserver-setup") < BSRV_HELPER_MIN:
        return f"на {p.name} нужен helper backupserver-setup {BSRV_HELPER_MIN[0]}.{BSRV_HELPER_MIN[1]} или новее"
    if ssh_of(p, now) is None:
        return f"{p.name} еще не сообщил порт и ключ SSH (helper backupserver-setup)"
    if not pick_ip(p):
        return f"у {p.name} нет адреса, по которому к нему придет резервный"
    return ""


def why_not_mirror(m, now: datetime | None = None) -> str:
    """Почему этот сервер пока не может быть резервным (пусто - может)."""
    if not agent_ok(m):
        return f"на {m.name} нужен агент {AGENT_MIN} или новее"
    rep = mirror_report(m, now)
    if rep is None:
        return f"на {m.name} не стоит helper mirror-setup"
    if _vt(rep.get("helper")) < MIRROR_HELPER_MIN:
        return f"на {m.name} нужен helper mirror-setup {'.'.join(map(str, MIRROR_HELPER_MIN))} или новее"
    if not str(rep.get("pubkey") or "").startswith("ssh-ed25519 "):
        return f"{m.name} не сообщил ключ резервного"
    if not pick_ip(m):
        return f"у {m.name} нет адреса, с которого он придет к основному"
    return ""


def _pull_due(mirror, primaries, now: datetime, seen_online) -> bool:
    report = mirror_report(mirror, now)
    if not report or report.get("running") or why_not_mirror(mirror, now):
        return False

    raw_sources = report.get("sources")
    if not isinstance(raw_sources, dict):
        return False
    sources = {src: source for src, source in raw_sources.items()
               if isinstance(source, dict) and isinstance(source.get("repos"), list) and source["repos"]}
    wanted = {
        (entry["src"], repo) for primary in primaries for repo, entry in entries(primary).items()
        if entry["mirror"] == mirror.id
    }
    # The agent's pull runs the whole configured list. Wait for the selected list to reach it.
    if wanted != {(src, repo) for src, source in sources.items() for repo in source["repos"] if isinstance(repo, str)}:
        return False
    copies = {(c["src"], c["repo"]): c for c in report.get("copies") or []
              if isinstance(c, dict) and isinstance(c.get("src"), str) and isinstance(c.get("repo"), str)}
    for primary in primaries:
        if not primary.enabled or not seen_online(primary, now):
            continue
        acl = (bsrv_report(primary, now) or {}).get("mirrors") or {}
        allow = acl.get(mirror_name(mirror.id)) if isinstance(acl, dict) else None
        allowed = allow.get("repos") if isinstance(allow, dict) else None
        if not isinstance(allowed, list):
            continue
        primary_entries = entries(primary)
        for repo in (_rep(primary).get("backup_server") or {}).get("repos") or []:
            if not isinstance(repo, dict) or not isinstance(repo.get("name"), str):
                continue
            name = repo.get("name")
            entry = primary_entries.get(name)
            if not entry or entry["mirror"] != mirror.id or name not in allowed or sources[entry["src"]].get("pending"):
                continue
            if not repo.get("valid") or not repo.get("snapshots"):
                continue
            copy = copies.get((entry["src"], name), {})
            copied = copy.get("src_newest")
            latest = repo.get("last_activity")
            if not copy.get("frozen") and isinstance(latest, int) and latest > (copied if isinstance(copied, int) else 0):
                return True
    return False


async def queue_pulls(session_factory, now: datetime, seen_online) -> None:
    """Request one pull per destination once a new restic snapshot is visible on a source."""
    async with session_factory() as session:
        # Stopping the last copy can leave JSON null instead of SQL NULL on older rows.
        configured = select(Server).where(cast(Server.backup_mirror, String) != "null")
        primaries = [s for s in await session.scalars(configured) if entries(s)]
        targets = plan(primaries)
        if not targets:
            return
        mirrors = await session.scalars(select(Server).where(Server.enabled.is_(True), Server.id.in_(targets)))
        by_id = {p.id: p for p in primaries}
        for mirror in mirrors:
            if not seen_online(mirror, now) or not _pull_due(mirror, [by_id[p] for p in targets[mirror.id]], now, seen_online):
                continue
            recent = await session.scalar(select(BackupCommand.id).where(
                BackupCommand.server_id == mirror.id, BackupCommand.action == "mirror", BackupCommand.mode == "pull",
                or_(BackupCommand.status.in_(("pending", "running")), BackupCommand.created_at > now - PULL_RETRY),
            ).limit(1))
            if recent is None:
                session.add(BackupCommand(server_id=mirror.id, action="mirror", mode="pull", created_at=now))
        await session.commit()


def allow_payload(mirror, repos: list[str], now: datetime | None = None) -> dict:
    """Команда mirror_allow основному: что резервному можно читать. Пустой список - доступ убрать."""
    p: dict = {"name": mirror_name(mirror.id), "repos": sorted(set(repos))}
    if repos:
        rep = mirror_report(mirror, now) or {}
        p["client_ip"] = pick_ip(mirror)
        p["pubkey"] = str(rep.get("pubkey") or "")
    return p


def set_payload(mirror_id: int, servers, now: datetime | None = None) -> dict:
    """Команда mirror (mode set) резервному: все основные, с которых он копирует, и что именно."""
    byid = {s.id: s for s in servers}
    sources = []
    for pid, repos in sorted((plan(servers).get(mirror_id) or {}).items()):
        p = byid.get(pid)
        ssh = ssh_of(p, now) if p is not None else None
        if p is None or ssh is None or not pick_ip(p):
            continue
        src = entries(p)[repos[0]]["src"]
        sources.append({"name": src, "host": pick_ip(p), "port": int(ssh["port"]),
                        "hostkey": str(ssh["hostkey"]), "repos": repos})
    return {"mode": "set", "sources": sources}


def reported_mirror_ids(primary, now: datetime | None = None) -> set[int]:
    """Резервные, которым основной сейчас открыт (по его отчету): им тоже надо сказать, если их
    больше нет в настройках."""
    out = set()
    for name in ((bsrv_report(primary, now) or {}).get("mirrors") or {}):
        m = re.fullmatch(r"mirror-(\d+)", str(name))
        if m:
            out.add(int(m.group(1)))
    return out


def _fmt_age(sec: float) -> str:
    d = int(sec // 86400)
    return f"{d} дн." if d >= 1 else f"{max(1, int(sec // 3600))} ч"


def copy_problems(rep: dict, now: datetime) -> dict[str, tuple[int, str]]:
    """Беды копий резервного по блоку его helper'а: "src/repo" -> (уровень, текст). Уровень 2 - копия
    не обновляется или заморожена, 1 - испорченные файлы. Копии на паузе (сняты с копирования в
    панели) - не беда: их просто держат."""
    out: dict[str, tuple[int, str]] = {}
    ts = now.timestamp()
    cur = str(rep.get("current") or "") if rep.get("running") else ""
    for c in rep.get("copies") or []:
        if not isinstance(c, dict):
            continue
        key = f"{c.get('src')}/{c.get('repo')}"
        if c.get("frozen"):
            out[key] = (2, f"{key}: копия заморожена ({frozen_text(c)}), ничего не удаляется и не забирается")
            continue
        if not c.get("enabled"):
            continue
        last_ok = float(c.get("last_ok") or 0)
        added = float(c.get("added") or 0)
        err = err_text(c)
        if key != cur:
            if last_ok and ts - last_ok > STALE_S:
                out[key] = (2, f"{key}: копия отстала на {_fmt_age(ts - last_ok)}" + (f": {err}" if err else ""))
                continue
            if not last_ok and added and ts - added > FIRST_S:
                out[key] = (2, f"{key}: первая копия так и не забрана" + (f": {err}" if err else ""))
                continue
        if c.get("verify_status") == "failed":
            out[key] = (2, f"{key}: проверка восстановления второй копии не прошла")
            continue
        bad = int(c.get("check_bad") or 0)
        if bad:
            out[key] = (1, f"{key}: на резервном испортились файлы копии ({bad}), они отложены и заберутся заново")
            continue
        if int(c.get("bad_new") or 0):
            out[key] = (1, f"{key}: основной отдал файлы, не совпавшие с хешем ({int(c['bad_new'])}): на нем они "
                           "испорчены или подменены")
            continue
        # Older helpers have no restore probe. Unknown is explicitly a warning,
        # never evidence that the destination can be restored.
        if "verify_status" in c:
            status = c.get("verify_status")
            verified = float(c.get("verify_ts") or 0)
            if status != "ok" or not verified or not c.get("verify_files") or not c.get("verify_snapshot"):
                why = ("на резервном не настроен пароль восстановления" if c.get("verify_code") == "password_missing"
                       else "результат восстановления второй копии неизвестен")
                out[key] = (1, f"{key}: {why}")
            elif ts - verified > VERIFY_STALE_S:
                out[key] = (1, f"{key}: восстановление второй копии не проверялось больше 10 дней")
    return out


_FROZEN = {
    "snapshots": "на основном разом пропала большая часть снапшотов",
    "data": "на основном разом пропала большая часть данных",
    "empty": "репозиторий на основном пуст",
    "config_gone": "на основном пропал config репозитория",
    "config_changed": "на основном сменился config: это другой репозиторий",
    "repo_gone": "репозиторий исчез с основного сервера",
}


def frozen_text(c: dict) -> str:
    why = str(c.get("frozen_why") or "")
    code, _, detail = why.partition(" ")
    if code not in _FROZEN:
        return why
    # helper пишет "snapshots 15 of 17 ..." или "data <байт> of <байт> ...": сколько из скольких
    m = re.match(r"(\d+) of (\d+)", detail)
    if code in ("snapshots", "data") and m:
        a, b = int(m.group(1)), int(m.group(2))
        if code == "data":
            return f"{_FROZEN[code]} ({a / 1e9:.1f} из {b / 1e9:.1f} ГБ)"
        return f"{_FROZEN[code]} ({a} из {b})"
    return _FROZEN[code]


_ERR = {
    "ssh": "основной не пустил по SSH",
    "list": "список файлов с основного пришел неполным",
    "get": "перекачка оборвалась",
    "integrity": "в полученной копии отсутствуют или повреждены файлы",
    "space": "на резервном не хватает места",
    "config": "нет адреса или ключа основного",
    "run": "перекачка упала",
}


def err_text(c: dict) -> str:
    code = str(c.get("err_code") or "")
    if not code:
        return ""
    if code == "frozen":
        return "копия заморожена"
    raw = str(c.get("err") or "").strip().rstrip(".")
    return _ERR.get(code, code) + (f" ({raw[:160]})" if raw else "")


def problems(srv, now: datetime) -> dict[str, tuple[int, str]]:
    """Беды копий резервного сервера (пусто, если он не резервный или отчета нет)."""
    rep = mirror_report(srv, now)
    return copy_problems(rep, now) if rep is not None else {}
