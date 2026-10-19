"""Вкладка "Чистка" сервера бэкапов: что не так с чисткой репозиториев и что с этим сделать.

Сами пункты панель видела и раньше (helper backupserver-setup: кто чистит каждый репозиторий,
старый общий скрипт /etc/systemd-rest.conf, группы снапшотов, итог прогонов чистки), но "что
делать" не говорила: красная плашка про старый скрипт, абзац из 18 имен через запятую и две строки
про один и тот же репозиторий. Теперь у каждого пункта готовые шаги (fix) под этот сервер и эти
репозитории, тексты шагов - во фронте по id (как у обновлений ОС и старого прокси).

Панель сама ничего не применяет: у нее нет примитива, который удаляет бэкапы, и взлом панели не
должен их стирать. Команды выполняет человек на сервере бэкапов. Удаление снапшотов необратимо,
поэтому у всего, что удаляет, сначала пробный прогон (helper без --apply), потом применение,
потом проверка; где можно, откат.
"""
from __future__ import annotations

import re
from datetime import datetime

from app.collector import (
    _BACKUP_REPO_STALE_SECONDS,
    archived_repo,
    bsrv_extra,
    old_groups,
    prune_off,
    prune_problems,
    renamed_to,
    repo_groups,
    rotation_reasons,
)

# раскладка сервера бэкапов, та же, что создает helper (backupserver-setup.sh)
HELPER = "sudo /lib65/kervax/kervax-backupserver-helper"
LEGACY = "/etc/systemd-rest.conf"
PRUNE_DIR = "/app/rest-server/system/scripts"
ENV_DIR = "/app/rest-server/system/envs"
LOG_DIR = "/app/rest-server/system/logs"
# имя репозитория, хоста: как valid_name у helper'а. Остальное в команду не подставляем
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# с какой версии helper'а есть команда
ADOPT_VER = (0, 25)   # adopt-legacy: перенос со старого скрипта, блоки на удаленные репозитории
GROUP_VER = (0, 28)   # forget-group: удалить старую группу снапшотов
MERGE_VER = (0, 33)   # merge-group: склеить переименованный сервер с прежним именем
OFF_VER = (0, 34)     # prune-off / prune-on; adopt-legacy переносит и "чистка выключена"


def _ver(rep: dict) -> tuple[int, ...]:
    v = str(((rep.get("setup_versions") or {}).get("backupserver-setup")) or "")
    return tuple(int(x) for x in re.findall(r"\d+", v)[:2])


def _update(need: tuple[int, int]) -> dict:
    return {"id": "helper_update", "arg": f"{need[0]}.{need[1]}"}


def _ok(names: list[str]) -> list[str]:
    return [n for n in names if isinstance(n, str) and _NAME.match(n)]


def _restore() -> str:
    # самая свежая копия, которую helper делает перед каждой правкой старого скрипта
    return f'sudo cp -p "$(ls -t {LEGACY}.kervax-bak-* | head -n1)" {LEGACY}'


def _run(names: list[str]) -> str:
    """Запустить чистку репозиториев сейчас и показать итог прогона (строка done: success,
    снапшотов было и стало, сколько удалено)."""
    return "\n".join(f"sudo {PRUNE_DIR}/restic-prune-{n}.sh\nsudo grep '^=== ' {LOG_DIR}/restic-prune-{n}.log | tail -n 1"
                     for n in names)


def adopt_fix(names: list[str], ver: tuple[int, ...], scheduled: bool = True) -> list[dict]:
    """Перевести репозитории со старого скрипта на чистку панели (adopt-legacy): у каждого свой
    prune-скрипт с той же политикой и паролем из его блока, cron на ночь, блок в старом скрипте
    закомментирован, копия файла рядом. Снапшоты удаляет только первая чистка.

    scheduled=False - старый скрипт не запускается (его строку в cron закомментировали). helper 0.34
    переносит и это: чистка у перенесенных остается выключенной, включает ее отдельный шаг
    prune-on. Старый helper включил бы ее сам при переносе, поэтому сначала обновить его."""
    names = _ok(names)
    if not names:
        return []
    if ver < ADOPT_VER:
        return [_update(ADOPT_VER)]
    if not scheduled and ver < OFF_VER:
        return [_update(OFF_VER)]
    ns = " ".join(names)
    steps = [
        {"id": "adopt_dry", "cmd": f"{HELPER} adopt-legacy {ns}"},
        {"id": "adopt_apply" if scheduled else "adopt_apply_off", "cmd": f"{HELPER} adopt-legacy --apply {ns}"},
        {"id": "adopt_check", "cmd": "cat " + " ".join(f"/etc/cron.d/kervax-prune-{n}" for n in names)
         + f"\nsudo grep -c '^# kervax-moved RESTIC_REPOSITORY=' {LEGACY}"},
    ]
    if not scheduled:
        steps.append({"id": "adopt_on", "cmd": "\n".join(f"{HELPER} prune-on {n}" for n in names)})
    return steps + [
        {"id": "adopt_run", "cmd": _run(names)},
        {"id": "adopt_undo", "undo": True, "cmd": _restore() + "\nsudo rm -f " + " ".join(
            f"/etc/cron.d/kervax-prune-{n} {PRUNE_DIR}/restic-prune-{n}.sh {ENV_DIR}/{n}.env" for n in names)},
    ]


def dead_fix(names: list[str], ver: tuple[int, ...]) -> list[dict]:
    """Закомментировать в старом скрипте блоки репозиториев, которых больше нет: тот же
    adopt-legacy, только с их именами (для удаленного репозитория он ничего не переносит)."""
    names = _ok(names)
    if not names:
        return []
    if ver < ADOPT_VER:
        return [_update(ADOPT_VER)]
    ns = " ".join(names)
    return [
        {"id": "dead_dry", "cmd": f"{HELPER} adopt-legacy {ns}"},
        {"id": "dead_apply", "cmd": f"{HELPER} adopt-legacy --apply {ns}"},
        {"id": "dead_check", "arg": str(len(names)),
         "cmd": f"sudo sh -n {LEGACY} && echo OK\nsudo grep -c '^# kervax-gone RESTIC_REPOSITORY=' {LEGACY}"},
        {"id": "dead_undo", "undo": True, "cmd": _restore()},
    ]


def group_fix(repo: str, host: str, to: str, ver: tuple[int, ...]) -> dict:
    """Старая группа снапшотов (хост, который сюда больше не бэкапится): удалить ее или, если это
    прежнее имя переименованного сервера, отдать новому имени. Политика такую группу не чистит:
    последние снапшоты каждой группы она держит всегда."""
    if not (_NAME.match(repo or "") and _NAME.match(host or "")):
        return {}
    check = {"id": "group_check", "cmd": f"{HELPER} forget-group {repo} {host}"}
    out: dict = {}
    if ver < GROUP_VER:
        out["forget"] = [_update(GROUP_VER)]
    else:
        out["forget"] = [
            {"id": "group_dry", "cmd": f"{HELPER} forget-group {repo} {host}"},
            {"id": "group_apply", "cmd": f"{HELPER} forget-group {repo} {host} --apply"},
            check,
            {"id": "group_keep", "alt": True},
        ]
    if to and _NAME.match(to) and to != host:
        if ver < MERGE_VER:
            out["merge"] = [_update(MERGE_VER)]
        else:
            out["merge"] = [
                {"id": "merge_dry", "arg": to, "cmd": f"{HELPER} merge-group {repo} {host} {to}"},
                {"id": "merge_apply", "arg": to, "cmd": f"{HELPER} merge-group {repo} {host} {to} --apply"},
                check,
            ]
    return out


def script_fix(repo: str, r: dict) -> list[dict]:
    """Своя чистка идет, а старье не уходит: что она делала и что удалит политика сейчас (forget
    --dry-run без блокировки репозитория, тем же restic и с той же группировкой, что и чистка)."""
    if not _NAME.match(repo):
        return []
    keep = " ".join(f"--keep-{k} {int(r.get(f'keep_{k}') or 0)}"
                    for k in ("last", "daily", "weekly", "monthly") if int(r.get(f"keep_{k}") or 0) > 0)
    script = f"{PRUNE_DIR}/restic-prune-{repo}.sh"
    return [
        {"id": "script_log", "cmd": f"sudo grep -E '^=== |^(Fatal|ERROR|SAFETY|BUSY)' {LOG_DIR}/restic-prune-{repo}.log | tail -n 12"},
        {"id": "script_dry", "cmd": f"sudo bash -c 'set -a; . {ENV_DIR}/{repo}.env; set +a; "
                                    f"B=$(sed -n \"s/^BIN=//p\" {script}); "
                                    f"\"$B\" forget --dry-run --no-lock --group-by host,tags {keep}'"},
        {"id": "script_run", "cmd": _run([repo])},
    ]


def prune_fix(repo: str, kind: str) -> list[dict]:
    """Чистка репозитория не работает: не открывает его, падает, пропущена или не запускается."""
    if not _NAME.match(repo):
        return []
    log = f"{LOG_DIR}/restic-prune-{repo}.log"
    script = f"{PRUNE_DIR}/restic-prune-{repo}.sh"
    run = {"id": "prune_run", "cmd": _run([repo])}
    if kind == "access":
        return [{"id": "prune_log", "cmd": f"sudo tail -n 20 {log}"},
                {"id": "prune_env", "cmd": f"sudoedit {ENV_DIR}/{repo}.env"}, run]
    if kind == "failed":
        return [{"id": "prune_log", "cmd": f"sudo tail -n 40 {log}"}, run]
    if kind == "skipped":
        return [{"id": "prune_policy", "cmd": f"sudo grep -n '^KEEP=' {script}\nsudoedit {script}"}, run]
    if kind == "idle":
        return [{"id": "prune_cron", "cmd": f"cat /etc/cron.d/kervax-prune-{repo}\n"
                                            f"sudo grep -h 'restic-prune-{repo}' /var/log/syslog /var/log/cron 2>/dev/null | tail -n 3"},
                run]
    return []


def off_fix(repo: str, r: dict) -> list[dict]:
    """Чистку репозитория выключили (prune-off, закомментированная строка cron): как включить.
    Сначала что удалит первая чистка (без удаления), потом prune-on, проверка, откат prune-off."""
    if not _NAME.match(repo):
        return []
    return [{**script_fix(repo, r)[1], "id": "off_dry"},
            {"id": "off_on", "cmd": f"{HELPER} prune-on {repo}"},
            {"id": "off_check", "cmd": f"cat /etc/cron.d/kervax-prune-{repo}"},
            {"id": "off_undo", "undo": True, "cmd": f"{HELPER} prune-off {repo}"}]


def hand_fix(repo: str) -> list[dict]:
    """Чистка идет, а в crontab root ее строка закомментирована руками: так ее выключали раньше, и
    helper до 0.34 включил ее обратно сам. Решить: выключить (prune-off) или оставить (prune-on
    запоминает, что включили осознанно, и пункт уходит)."""
    if not _NAME.match(repo):
        return []
    return [{"id": "hand_look", "cmd": f"sudo crontab -l -u root | grep -n 'restic-prune-{repo}.sh'\n"
                                       f"cat /etc/cron.d/kervax-prune-{repo}"},
            {"id": "hand_off", "cmd": f"{HELPER} prune-off {repo}"},
            {"id": "hand_keep", "alt": True, "cmd": f"{HELPER} prune-on {repo}"}]


def view(s, now: datetime) -> dict | None:
    """Чистка сервера бэкапов для карточки: {legacy, dead, rotation, prune, groups}, None - это не
    сервер бэкапов.

    legacy - живые (не в архиве) репозитории на старом общем скрипте, запланирован ли он и шаги
    переноса на чистку панели; dead - его блоки на удаленные репозитории; rotation - где старье не
    вычищается, одна строка на репозиторий со всеми причинами и тем, отчего так (cause); prune -
    где не работает своя чистка; groups - шаги по старым группам снапшотов: repo -> host ->
    {forget, merge}; off - где чистку выключили (live - бэкапы при этом идут); hand - где чистка
    идет, хотя ее строку в crontab root закомментировали руками."""
    rep = s.last_report or {}
    bs = rep.get("backup_server") or {}
    if not bs.get("present"):
        return None
    ext = bsrv_extra(rep)
    arch = getattr(s, "backup_repo_archive", None) or {}
    ver = _ver(rep)
    nowts = now.timestamp()
    obs = (ext or {}).get("repos") or {}
    leg = (ext or {}).get("legacy")
    leg = leg if isinstance(leg, dict) else None
    scheduled = bool(leg and leg.get("where"))
    stats = {str(r.get("name") or ""): r for r in bs.get("repos") or [] if isinstance(r, dict)}
    reasons = rotation_reasons(s, now)
    out: dict = {"legacy": None, "dead": None, "rotation": [], "prune": [], "groups": {}, "off": [], "hand": []}
    if leg:
        script = str(leg.get("script") or LEGACY)
        on = sorted(n for n, o in obs.items() if isinstance(o, dict) and o.get("cleaner") == "legacy")
        # в архиве - то, что решили хранить как есть: новых снапшотов нет, чистить нечего
        live = [n for n in on if not archived_repo(arch, n)]
        if live:
            out["legacy"] = {
                "script": script, "where": str(leg.get("where") or ""), "schedule": str(leg.get("schedule") or ""),
                "log_ts": int(leg.get("log_ts") or 0), "scheduled": scheduled,
                "repos": [{"name": n, "why": reasons.get(n, []), "snaps": int((stats.get(n) or {}).get("snapshots") or 0)}
                          for n in live],
                "archived": len(on) - len(live),
                "fix": adopt_fix(live, ver, scheduled),
            }
        missing = _ok(list(leg.get("missing") or []))
        if missing:
            out["dead"] = {"script": script, "scheduled": scheduled, "repos": missing, "fix": dead_fix(missing, ver)}
    for name, why in reasons.items():
        o = obs.get(name) if isinstance(obs.get(name), dict) else {}
        olds = old_groups(ext, arch, name, nowts)
        cleaner = o.get("cleaner") if ext and o else None
        if olds:
            cause, fix = "old_group", []  # шаги - у каждой группы (groups)
        elif cleaner == "legacy":
            cause, fix = ("legacy" if scheduled else "legacy_off"), adopt_fix([name], ver, scheduled)
        elif cleaner == "script":
            cause, fix = "script", script_fix(name, stats.get(name) or {})
        elif cleaner == "":
            cause, fix = "none", [{"id": "none_setup"}]
        else:
            # helper старше 0.23 или встал: кто чистит, не видно
            cause, fix = "unknown", [_update((0, 23))]
        out["rotation"].append({"repo": name, "reasons": why, "cause": cause,
                                "hosts": [str(g.get("host") or "") for g in olds], "fix": fix})
    for name in obs:
        groups = repo_groups(ext, name, nowts)
        for g in old_groups(ext, arch, name, nowts):
            host = str(g.get("host") or "")
            fx = group_fix(name, host, renamed_to(groups, g), ver)
            if fx:
                out["groups"].setdefault(name, {})[host] = fx
    # чистку выключили (helper 0.34): решение, а не поломка, в ротацию и алерты не идет. Бэкапы туда
    # идут (live) - репозиторий растет без чистки, это отдельный пункт с шагами включить; архив или
    # бэкапов давно нет - просто строка "выключена", чистить там нечего
    for name in sorted(n for n in obs if prune_off(ext, n)):
        if not _NAME.match(name):
            continue
        o = obs[name]
        st = stats.get(name) or {}
        last = int(st.get("last_activity") or 0)
        live = not archived_repo(arch, name) and last > 0 and nowts - last <= _BACKUP_REPO_STALE_SECONDS
        out["off"].append({"repo": name, "live": live, "since": int(o.get("prune_off_ts") or 0),
                           "why": str(o.get("prune_off_why") or "")[:200], "last": last,
                           "snaps": int(st.get("snapshots") or 0), "fix": off_fix(name, st)})
    # чистка идет, хотя ее строку в crontab выключили руками: вопрос, а не поломка
    for name in sorted(n for n, o in obs.items() if isinstance(o, dict) and int(o.get("prune_hand_off") or 0) == 1):
        if _NAME.match(name):
            out["hand"].append({"repo": name, "fix": hand_fix(name)})
    # те же репозитории, что bsrv_prune и алерт backup_prune
    for name, (kind, why) in sorted(prune_problems(bs, ext, now, set(s.backup_repo_mutes or []), arch).items()):
        out["prune"].append({"repo": name, "kind": kind, "why": why, "fix": prune_fix(name, kind)})
    return out
