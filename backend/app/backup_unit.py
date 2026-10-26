"""Старый runner бэкапа: restic прямо в ExecStart юнита (или в своем скрипте), а не скрипт панели.

На таких нодах get-config читает INCLUDE_MODE/EXCLUDES_STR из /etc/systemd-rest.conf, которого юнит
не запускает (или которого нет вовсе), и панель показывала exclude с пустыми списками, т.е. "бэкапится
весь /", и предлагала менять пути, которые не применятся. helper backup-setup 0.45 разбирает сам юнит
в extras["backup-unit"]: {"runner": "unit"|"panel", "service", "source" (unit|script), "script",
"parsed", "mode", "includes", "excludes", "exclude_files", "files_from", "flags", "adopt"}.

При приеме отчета пути из юнита подменяют пути get-config в блоке backup: аудит покрытия, GitLab и
карточка видят то, что бэкапится на самом деле. Не разобрали юнит (путь в переменной, нет списка
--files-from) - пути неизвестны, и про покрытие панель ничего не утверждает."""

KEY = "backup-unit"
HELPER = "0.45"  # backup-setup, с которой приходит extras["backup-unit"]
ADOPT_CMD = "sudo /lib65/kervax/kervax-backup-helper adopt"
ADOPT_DRY_CMD = "sudo KV_ADOPT_DRYRUN=1 /lib65/kervax/kervax-backup-helper adopt"
_LIST_MAX = 200


def _strs(v) -> list[str]:
    return [str(x)[:512] for x in (v if isinstance(v, list) else [])[:_LIST_MAX] if isinstance(x, str) and x]


def apply(rep: dict) -> None:
    """На месте: блок backup отчета получает runner ("unit"/"panel") и для runner'а юнита пути из юнита.
    unit_paths - что показать в карточке как есть (флаги, файлы списков, можно ли adopt)."""
    if not isinstance(rep, dict):
        return
    bk = rep.get("backup")
    ex = (rep.get("extras") or {}).get(KEY)
    if not isinstance(bk, dict) or not isinstance(ex, dict):
        return
    runner = ex.get("runner")
    if runner == "panel":
        bk["runner"] = "panel"
        return
    if runner != "unit":
        return
    parsed = ex.get("parsed") is True
    mode = ex.get("mode") if ex.get("mode") in ("include", "exclude") else ""
    bk["runner"] = "unit"
    bk["unit_paths"] = {
        "service": str(ex.get("service") or "")[:100],
        "source": "script" if ex.get("source") == "script" else "unit",
        "script": str(ex.get("script") or "")[:512],
        "parsed": parsed and bool(mode),
        "exclude_files": _strs(ex.get("exclude_files")),
        "files_from": _strs(ex.get("files_from")),
        "flags": _strs(ex.get("flags")),
        "adopt": ex.get("adopt") is True,
    }
    if parsed and mode:
        bk["mode"] = mode
        bk["includes"] = _strs(ex.get("includes")) if mode == "include" else []
        bk["excludes"] = _strs(ex.get("excludes")) if mode == "exclude" else []
    else:
        bk["mode"] = ""
        bk["includes"] = []
        bk["excludes"] = []


def paths_known(bk: dict | None) -> bool:
    """Знает ли панель, что бэкапится: у runner'а юнита - только если юнит разобран"""
    if not isinstance(bk, dict):
        return False
    if bk.get("runner") == "unit":
        return bool((bk.get("unit_paths") or {}).get("parsed"))
    return True


def paths_editable(bk: dict | None) -> bool:
    """Применятся ли пути из панели: у runner'а юнита их правит только сам юнит (или adopt)"""
    return isinstance(bk, dict) and bk.get("runner") != "unit"
