"""Почему не включился дамп базы: причина отказа не должна приходить пустой.

Кнопка "включить дамп" идет через helper backup-setup на ноде: он снимает пробный дамп и при
отказе отвечает "the dump probe failed, dumps were NOT enabled: <причина>". До 0.38 почти все
проверки отправляли ошибку в /dev/null вместе с выводом, и причина приходила пустой: в панели
под кнопкой после "дампы НЕ включены:" стоял прочерк. Так было на ноде с MySQL 8 на хосте: root
без пароля не пускали, а что упало, не видел никто.

Helper'ы на нодах обновляются не сразу, поэтому пустую причину от старого helper'а панель
дописывает сама, когда агент присылает результат: какая версия на ноде, что она теряет ошибку и
что ее надо обновить. Фраза в стиле helper'а (по-английски), на язык интерфейса ее переводит
фронт (frontend/src/helperText.ts) и добавляет готовые команды под эту панель.
"""

from __future__ import annotations

PROBE_PREFIX = "the dump probe failed, dumps were NOT enabled:"
# с этой версии helper всегда пишет причину отказа (agent/backup-setup.sh)
REASON_SINCE = "0.38"
_DUMP_ACTIONS = ("dump_setup", "dump_remove")


def _ver(v: object) -> tuple[int, ...]:
    """'0.37' -> (0, 37); покомпонентно, как в api/servers._ver_key. Нечисловое - древнее всего."""
    try:
        return tuple(int(p) for p in str(v or "").strip().split("."))
    except ValueError:
        return (0,)


def _no_reason(helper_ver: str, engine: str, container: str) -> str:
    ver = helper_ver or "unknown"
    # слот как у helper'а: движок или движок@контейнер (по нему фронт дает команду проверки руками)
    slot = f"{engine or 'unknown'}@{container}" if container else (engine or "unknown")
    head = f"no reason came from backup-setup {ver} for {slot}"
    if helper_ver and _ver(helper_ver) >= _ver(REASON_SINCE):
        return head
    out = f"{head}: helpers before {REASON_SINCE} lose the error of the check, update it"
    # Найденный случай: MySQL 8 на хосте пускает root только с паролем, а старый helper входил
    # только как root. С 0.38 он входит через debian.cnf или /root/.my.cnf
    if engine == "mysql" and not container:
        out += f" (MySQL on the host: {REASON_SINCE} also logs in through /etc/mysql/debian.cnf or /root/.my.cnf)"
    return out


def fill_dump_reason(action: str, ok: bool, output: str, payload: dict | None,
                     setup_versions: dict | None) -> str:
    """Результат команды дампа с непустой причиной отказа. Успех, другие команды и отказ с
    причиной возвращаются как пришли."""
    if ok or action not in _DUMP_ACTIONS:
        return output
    out = (output or "").strip()
    probe_empty = out.rstrip(" :") == PROBE_PREFIX.rstrip(" :")
    if out and not probe_empty:
        return output
    p = payload or {}
    helper_ver = str((setup_versions or {}).get("backup-setup") or "")
    reason = _no_reason(helper_ver, str(p.get("engine") or ""), str(p.get("container") or ""))
    return f"{PROBE_PREFIX} {reason}" if probe_empty else reason
