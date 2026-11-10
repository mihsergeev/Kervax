"""Home-page verification. No acknowledgment is evidence of recovery.

Request ids identify a new collection. Replayed reports, file mtimes alone and missing
blocks never prove recovery. Work and results live in SQL; GET resumes interrupted
panel probes and agent command polling retries leased read-only work.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update

from app import fixed, home_mute, probe_scope, tamper
from app.collector import _ALERT_SECTION, _aware, seen_online
from app.models import AlertRecheck, Check, Server, User

TIMEOUT = 180
PANEL_TIMEOUT = 60
LEASE = 75
RETRY = 30

# The client selects an existing issue, never a command, path or URL. Each source
# is implemented by a fixed read-only collector in the agent/root dispatcher.
SOURCES = {
    "kube_expiry": "kube_expiry", "flux_down": "kube_expiry", "flux_stale": "kube_expiry",
    "ssh_risk": "changes", "access_risk": "changes",
    "home:ssh-ssh_password": "changes", "home:access-s": "changes", "home:access-w": "changes",
    "home:sw-eol": "software", "home:sw-soon": "software",
    "home:d-acc": "docker", "home:d-old": "docker", "home:dockrisk": "docker",
    "home:noboot-d": "docker", "home:k-acc": "kube",
    "home:dbdump": "backup", "home:bk-http": "backup",
    "mem": "mem", "disk": "disk", "inode": "disk", "conntrack": "conntrack", "temp": "temp",
}
HOME_SECTION = {
    **{k: "servers" for k in home_mute.SIGS},
    **{k: "docker" for k in ("d-acc", "d-old", "dockrisk", "noboot-d")},
    **{k: "kuber" for k in ("k-acc", "k-noagent")},
    **{k: "backups" for k in ("dbdump", "bk-http", "bs-restic", "bs-setup")},
}


def section(object_type: str, kind: str) -> str:
    if object_type == "check":
        if kind not in {"tamper", "status", "ssl", "domain"}:
            raise HTTPException(422, "Неизвестный вид проверки")
        return "sites"
    if kind.startswith("home:"):
        if kind[5:] not in HOME_SECTION:
            raise HTTPException(422, "Неизвестный вид проверки")
        return HOME_SECTION[kind[5:]]
    from app.settings_store import SERVER_ALERT_KINDS
    if kind not in SERVER_ALERT_KINDS and kind != "offline":
        raise HTTPException(422, "Неизвестный вид проверки")
    if kind.startswith(("kube_", "flux_")):
        return "kuber"
    return _ALERT_SECTION.get(kind, "servers")


def identity(obj) -> str:
    fields = ("target", "type", "enabled", "probe_local", "probe_server_id", "parent_mode", "parent_server_id",
              "method", "http_headers",
              "auth_method", "auth_user", "auth_pass", "ignore_tls", "created_at",
              "expected_status", "keyword_up", "keyword_down", "ssl_warn_days", "domain_warn_days",
              "port", "timeout_ms", "degraded_ms", "retries", "check_all_ips", "extra_paths",
              "check_ssl", "check_domain", "dns_type", "dns_expect", "dns_match", "dns_server") if isinstance(obj, Check) else (
                  "token_hash", "enabled", "created_at", "alert_mutes", "db_dumps_ok", "known_issues",
                  *[c.key for c in Server.__table__.columns if "alert_" in c.key and c.key not in {"alert_state", "alert_fixed", "alert_mutes"}],
                  "kube_expiry_warn_days", "disk_warn_percent", "disk_crit_percent", "offline_after_seconds",
                  "backup_audit_mutes", "backup_not_required", "kube_node_ignored", "kube_expiry_ignored",
                  "queue_alert_over", "backup_repo_mutes", "backup_repo_archive")
    # Keep only the digest, never credentials or addresses, in the job.
    values = [getattr(obj, k, None) for k in fields]
    values = [_aware(v).isoformat() if isinstance(v, datetime) else v for v in values]
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def snapshot_fields(source: str) -> tuple[str, ...]:
    return {
        "kube_expiry": ("kube_expiry", "flux"), "changes": ("extras",),
        "software": ("docker", "db_stats"), "docker": ("docker",), "kube": ("kube",),
        "backup": ("backup", "db_engines", "db_stats", "extras"),
        "mem": ("mem_used", "mem_total"), "disk": ("disks",),
        "conntrack": ("conntrack_count", "conntrack_max"), "temp": ("cpu_temp",),
    }[source]


def scoped_snapshot(rep: dict, source: str) -> dict:
    out = {k: rep.get(k) for k in snapshot_fields(source)}
    if "extras" in out:
        keys = ("changes",) if source == "changes" else ("custom-backups",)
        out["extras"] = {k: (rep.get("extras") or {}).get(k) for k in keys}
    return out


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def check_snapshot(obj, kind: str) -> dict:
    if kind == "tamper":
        return {"tamper_state": obj.tamper_state}
    fields = (f"{kind}_days", f"{kind}_message", "expiry_checked_at") if kind in {"ssl", "domain"} else (
        "last_checked_at", "last_status", "last_message", "last_ip_results", "last_path_results")
    return {k: (_aware(v).isoformat() if isinstance(v := getattr(obj, k), datetime) else v) for k in fields}


def finish(job, state: str, message: str, now: datetime) -> None:
    job.status, job.message, job.done_at, job.active_key = state, message[:512], now, None


def expire(job, now: datetime) -> None:
    if job.status == "checking" and _aware(job.deadline) <= now:
        finish(job, "unavailable", "Не удалось проверить: нет свежих данных за отведенное время", now)


def public(job) -> dict:
    return {k: getattr(job, k) for k in (
        "id", "object_type", "object_id", "kind", "section", "created_at", "done_at", "status", "message")}


def spec_for(obj, kind: str) -> dict:
    spec = {"identity": identity(obj)}
    if isinstance(obj, Check):
        spec["baseline"] = digest(check_snapshot(obj, kind))
    if isinstance(obj, Server):
        rep = obj.last_report or {}
        spec["source"] = SOURCES.get(kind, "")
        if kind == "kube_expiry":
            # A disappeared entry can also mean an API/read failure. A renewed cert
            # at the same identity proves recovery; a missing one is unknown.
            from app import kube_expiry
            spec["required"] = [kube_expiry.key(it) for it in kube_expiry.live(obj, rep)]
        if spec["source"] in {"software", "backup"}:
            spec["db_stats"] = bool(rep.get("db_stats"))
            spec["docker"] = bool((rep.get("docker") or {}).get("present"))
    return spec


def unavailable(obj, kind: str, spec: dict, now: datetime) -> str:
    if not obj.enabled:
        return "Не удалось проверить: мониторинг объекта выключен"
    if isinstance(obj, Check):
        if probe_scope.uses_agent(obj):
            return "" if kind == "status" else "Не удалось проверить: для этой проверки изнутри сервера пока нет свежего разбора"
        if obj.type == "push":
            return "Не удалось проверить: push-монитор ждет новый сигнал от источника"
        return ""
    if not seen_online(obj, now):
        return "Не удалось проверить: сервер не на связи"
    if not spec["source"]:
        return "Не удалось проверить: для этого вида пока нет принудительной проверки, откройте карточку объекта"
    # Capability is stronger than a version string (also covers custom builds).
    caps = (obj.last_report or {}).get("caps") or {}
    if not caps.get("alert_recheck"):
        return "Не удалось проверить: обновите агент, нужна поддержка свежих проверок"
    if spec["source"] in {"kube_expiry", "changes", "backup"} or spec.get("db_stats"):
        versions = (obj.last_report or {}).get("setup_versions") or {}
        if not versions.get("recheck-setup"):
            return "Не удалось проверить: установите read-only helper recheck-setup"
        if caps.get("alert_recheck_root") is False:
            return "Не удалось проверить: очередь read-only helper недоступна агенту, переустановите recheck-setup"
        if spec["source"] == "backup":
            from app.collector import _ver_tuple
            if _ver_tuple(versions.get("backup-setup")) < (0, 50):
                return "Не удалось проверить: обновите backup-helper до 0.50, нужна read-only команда inspect"
    return ""


async def take_tasks(session, server: Server, now: datetime) -> list[dict]:
    if not ((server.last_report or {}).get("caps") or {}).get("alert_recheck"):
        return []
    rows = list(await session.scalars(select(AlertRecheck).where(
        AlertRecheck.object_type == "server", AlertRecheck.object_id == server.id,
        AlertRecheck.status == "checking",
    ).order_by(AlertRecheck.created_at).with_for_update(skip_locked=True)))
    out = []
    for job in rows:
        expire(job, now)
        if job.status != "checking":
            continue
        if job.taken_at and (now - _aware(job.taken_at)).total_seconds() < RETRY:
            continue
        if identity(server) != job.spec["identity"]:
            finish(job, "unavailable", "Не удалось проверить: настройки объекта изменились", now)
            continue
        job.taken_at = now
        if "baseline" not in job.spec:
            job.spec = {**job.spec, "baseline": digest(scoped_snapshot(server.last_report or {}, job.spec["source"]))}
        if job.spec["source"] == "software" and "host_baseline" not in job.spec:
            job.spec = {**job.spec, "host_baseline": digest([server.sw_host, server.pkg_hash])}
        out.append({"id": job.id, "source": job.spec["source"], "db_stats": bool(job.spec.get("db_stats"))})
        if len(out) >= 4:
            break
    return out


def apply_snapshot(server, job, snapshot: dict) -> None:
    """Validate required blocks, then merge ONLY the collector's allowed fields."""
    src = job.spec["source"]
    allowed = snapshot_fields(src)
    if any(k not in snapshot for k in allowed):
        raise ValueError("нужный блок не получен")
    if src == "kube_expiry":
        from app import kube_expiry
        if not isinstance(snapshot["kube_expiry"], list) or not isinstance(snapshot["flux"], list):
            raise ValueError("неполный ответ коллектора Kubernetes")
        have = {kube_expiry.key(it) for it in snapshot["kube_expiry"]}
        if not set(job.spec.get("required") or []).issubset(have):
            raise ValueError("сертификат исчез из отчета, обновление не подтверждено")
    if src in {"docker", "software"}:
        dk = snapshot["docker"]
        if not isinstance(dk, dict) or (dk.get("present") and not dk.get("access")):
            raise ValueError("нет доступа для чтения Docker")
    if src == "kube" and not (snapshot["kube"] or {}).get("access"):
        raise ValueError("нет доступа для чтения Kubernetes")
    if src == "changes" and not (((snapshot["extras"] or {}).get("changes") or {}).get("ssh") or {}).get("ports"):
        raise ValueError("нет свежего разбора SSH")
    if src == "changes":
        ssh = snapshot["extras"]["changes"]["ssh"]
        if ssh.get("pass") not in {"yes", "no"} or ssh.get("root") not in {"yes", "no", "prohibit-password", "without-password", "forced-commands-only"}:
            raise ValueError("нет полного разбора политики SSH")
    if src == "mem" and (not isinstance(snapshot["mem_total"], (int, float)) or snapshot["mem_total"] <= 0 or not 0 <= snapshot["mem_used"] <= snapshot["mem_total"]):
        raise ValueError("нет свежих метрик памяти")
    if src == "conntrack" and (snapshot["conntrack_max"] <= 0 or snapshot["conntrack_count"] < 0):
        raise ValueError("нет свежих метрик conntrack")
    if src == "temp" and not isinstance(snapshot["cpu_temp"], (int, float)):
        raise ValueError("нет свежей температуры")
    if src == "disk" and (not isinstance(snapshot["disks"], list) or not snapshot["disks"]):
        raise ValueError("нет свежего инвентаря дисков")
    if src == "backup" and job.spec.get("db_stats") and not snapshot["db_stats"] and snapshot["db_engines"]:
        raise ValueError("работающая СУБД не отдала свежий инвентарь")
    current = digest(scoped_snapshot(server.last_report or {}, src))
    incoming = digest(scoped_snapshot(snapshot, src))
    if current != job.spec.get("baseline") and current != incoming:
        raise ValueError("автоматический сбор уже изменил нужный блок; его нельзя затирать")
    rep = dict(server.last_report or {})
    for k in allowed:
        if k == "extras":
            keys = ("changes",) if src == "changes" else ("custom-backups",)
            new = {key: snapshot[k][key] for key in keys if key in snapshot[k]}
            rep[k] = {**(rep.get(k) or {}), **new}
        else:
            rep[k] = snapshot[k]
    if src == "software":
        from app import software
        if not isinstance(snapshot.get("packages"), list) or not snapshot["packages"]:
            raise ValueError("нет свежего списка пакетов")
        if not isinstance(snapshot.get("pkgs"), dict) or not snapshot["pkgs"].get("h"):
            raise ValueError("нет отпечатка свежего списка пакетов")
        if job.spec.get("db_stats") and not snapshot["db_stats"]:
            raise ValueError("нет свежего инвентаря СУБД")
        new = software.host_versions(snapshot["packages"])
        if digest([server.sw_host, server.pkg_hash]) != job.spec["host_baseline"] and digest([new, snapshot["pkgs"]["h"]]) != digest([server.sw_host, server.pkg_hash]):
            raise ValueError("уже получен более свежий список пакетов")
        server.sw_host = new
        rep["pkgs"] = snapshot["pkgs"]
    server.last_report = rep


def server_left(server, kind: str, now: datetime) -> str | None:
    if kind.startswith("home:"):
        sig = home_mute._sig(kind[5:], server, now, {})
        return None if sig is None else ("Проблема осталась" if sig else "")
    return fixed.left(server, kind, now)


async def ingest(session, server, job_id: str, ok: bool, snapshot: dict, now: datetime) -> None:
    job = await session.scalar(select(AlertRecheck).where(AlertRecheck.id == job_id).with_for_update())
    if job is None or job.object_type != "server" or job.object_id != server.id:
        raise HTTPException(404, "Проверка не найдена")
    expire(job, now)
    if job.status != "checking":
        return  # duplicate/late answers never rewrite the recorded verdict
    if job.taken_at is None or identity(server) != job.spec["identity"]:
        finish(job, "unavailable", "Не удалось проверить: ответ не относится к текущим настройкам", now)
        return
    if not ok:
        finish(job, "unavailable", "Не удалось проверить: коллектор не получил свежие данные", now)
        return
    # Do not leave a partially merged snapshot on an invalid answer.
    old, old_sw = server.last_report, server.sw_host
    try:
        apply_snapshot(server, job, snapshot)
        left = server_left(server, job.kind, now)
        if left is None:
            raise ValueError("нужный результат не получен")
    except (ValueError, TypeError, KeyError, AttributeError):
        server.last_report, server.sw_host = old, old_sw
        finish(job, "unavailable", "Не удалось проверить: нет полного свежего результата нужной проверки", now)
        return
    if left:
        finish(job, "remains", "Проблема осталась", now)
    else:
        # The normal collector closes the announced alert and sends its recovery.
        # Existing fixed support only skips recovery hold for a verified clean state;
        # no event windows are reset and no alert mutes are added.
        if not job.kind.startswith("home:"):
            server.alert_fixed = {**(server.alert_fixed or {}), job.kind: {**fixed.new_entry("Свежая проверка", job.requested_by, now), "verified": True}}
        finish(job, "fixed", "Исправление подтверждено свежей проверкой", now)
    if job.spec["source"] == "software":
        from app.models import ServerPackages
        pk = await session.get(ServerPackages, server.id)
        if pk is None:
            pk = ServerPackages(server_id=server.id)
            session.add(pk)
        meta = snapshot["pkgs"]
        pk.items, pk.hash, pk.mgr, pk.n, pk.ts, pk.pending = snapshot["packages"], meta["h"], meta["mgr"], len(snapshot["packages"]), now, None
        server.pkg_hash = meta["h"]


async def panel_work(factory, job_id: str) -> None:
    now = datetime.now(timezone.utc)
    async with factory() as session:
        claimed = await session.execute(update(AlertRecheck).where(
            AlertRecheck.id == job_id, AlertRecheck.status == "checking",
            (AlertRecheck.taken_at.is_(None)) | (AlertRecheck.taken_at < now - timedelta(seconds=LEASE)),
            AlertRecheck.deadline > now,
        ).values(taken_at=now))
        await session.commit()
        if not claimed.rowcount:
            return
        job = await session.get(AlertRecheck, job_id)
        obj = await session.get(Check, job.object_id)
        if obj is None or identity(obj) != job.spec["identity"]:
            finish(job, "unavailable", "Не удалось проверить: объект удален или настройки изменились", now)
            await session.commit()
            return
        if job.kind != "tamper":
            # Standard check-run is used by the API before creating a pending job
            # for agent-local monitors. External probes use the same execution path.
            from app import checks as checks_exec
            from app.collector import record_outcome, send_alerts_soon
            try:
                if job.kind in {"ssl", "domain"}:
                    checks_exec.forget_domain(obj.target)
                    info = await asyncio.wait_for(checks_exec.probe_expiry(obj), PANEL_TIMEOUT)
                    value = info.ssl_days if job.kind == "ssl" else info.domain_days
                    if value is None:
                        raise ValueError("нет свежего срока")
                else:
                    outcome = await asyncio.wait_for(checks_exec.run_check(obj), PANEL_TIMEOUT)
                    probe_scope.explain_panel_fallback(obj, outcome)
            except Exception:
                await session.rollback()
                job = await session.get(AlertRecheck, job_id)
                if job and job.status == "checking":
                    finish(job, "unavailable", "Не удалось проверить: нет свежего результата", datetime.now(timezone.utc))
                await session.commit()
                return
            await session.rollback()
            job = await session.scalar(select(AlertRecheck).where(AlertRecheck.id == job_id).with_for_update())
            obj = await session.scalar(select(Check).where(Check.id == job.object_id).with_for_update())
            now = datetime.now(timezone.utc)
            expire(job, now)
            pending = []
            if job.status != "checking":
                await session.commit()
                return
            if obj is None or identity(obj) != job.spec["identity"]:
                finish(job, "unavailable", "Не удалось проверить: настройки объекта изменились", now)
            elif digest(check_snapshot(obj, job.kind)) != job.spec["baseline"]:
                finish(job, "unavailable", "Не удалось проверить: уже получены более новые автоматические данные", now)
            else:
                if job.kind in {"ssl", "domain"}:
                    setattr(obj, f"{job.kind}_days", value)
                    setattr(obj, f"{job.kind}_message", (info.ssl_message if job.kind == "ssl" else info.domain_message)[:256])
                    obj.expiry_checked_at = now
                    from app import expiry_snooze
                    expiry_snooze.sync(obj)
                    thresholds = getattr(obj, f"{job.kind}_warn_days") or []
                    left = any(value <= d for d in thresholds)
                else:
                    await record_outcome(session, obj, outcome, now, pending, manual=True)
                    left = outcome.status != "up"
                finish(job, "remains" if left else "fixed", "Проблема осталась" if left else "Исправление подтверждено свежей проверкой", now)
            await session.commit()
            if pending:
                await send_alerts_soon(factory, pending, now)
            return
        try:
            result = await asyncio.wait_for(tamper.scan(obj), PANEL_TIMEOUT)
        except Exception:
            result = {"err": "не удалось получить страницу"}
        # Re-read under a row lock: a scheduler/config edit may have happened while
        # the network was running. Never save over its newer configuration/state.
        await session.rollback()
        job = await session.scalar(select(AlertRecheck).where(AlertRecheck.id == job_id).with_for_update())
        obj = await session.scalar(select(Check).where(Check.id == job.object_id).with_for_update())
        now = datetime.now(timezone.utc)
        expire(job, now)
        if job.status != "checking":
            await session.commit()
            return
        if obj is None or identity(obj) != job.spec["identity"]:
            finish(job, "unavailable", "Не удалось проверить: настройки объекта изменились", now)
        elif digest(check_snapshot(obj, job.kind)) != job.spec["baseline"]:
            finish(job, "unavailable", "Не удалось проверить: уже получены более новые автоматические данные", now)
        elif "obs" not in result:
            finish(job, "unavailable", "Не удалось проверить: страница недоступна или закрыта защитой", now)
        else:
            obs = result["obs"]
            before = obj.tamper_state or {}
            engine = before.get("engine") or {}
            if any(k not in (obs.get("engine") or {}) for k in engine) or set((before.get("signs") or {})) - {"old", "empty", "title", "dom", "bot"}:
                finish(job, "unavailable", "Не удалось проверить: страница не подтверждает версии или состояние файлов, нужен свежий разбор на сервере", now)
            else:
                vers = await tamper.versions(session, now)
                if ("wp" in engine and (obs.get("engine") or {}).get("wp") not in (vers.get("wp") or {})) or (
                        "php" in engine and not (vers.get("php") or {}).get("rel")):
                    finish(job, "unavailable", "Не удалось проверить: нет сведений о безопасности новой версии", now)
                    await session.commit()
                    return
                states = (await session.execute(select(Check.id, Check.tamper_state))).all()
                obj.tamper_state = tamper.observe(before, obs, now, tamper.fleet_domains(states, obj.id), vers)
                view = tamper.view(obj.tamper_state)
                left = bool(view and view["level"] != "none")
                finish(job, "remains" if left else "fixed", "Проблема осталась" if left else "Исправление подтверждено свежей проверкой", now)
        await session.commit()
