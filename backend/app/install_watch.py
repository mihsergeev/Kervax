"""Окно установки агента: открыт ли адрес новой ноды в фаерволе панели и что нода уже скачала.

Фаервол хоста панели правит не она, а ops/agent-firewall-sync.sh (крон и path-юнит): панель
пишет data/agent_allow_ips, скрипт открывает адреса и пишет назад agent_allow_ips.applied -
какой фаервол и что открыто сейчас. Файла нет - скрипт не стоит (панель за вайтлистом caddy,
где /api/agent/* открыт, или без фаервола), и сказать про адрес нечего.

Что нода уже скачала (установщик, бинарь агента), панель помнит по адресу запроса, в памяти
процесса: это подсказка для окна установки, после перезапуска ее не жалко. Через прокси и
ретранслятор запрос приходит с их адреса, такие попытки ноде не приписываются."""

import ipaddress
import os
import time

# ip -> {"script": ts, "agent": ts}
_SEEN: dict[str, dict[str, float]] = {}
_SEEN_MAX = 4096
_SEEN_TTL = 86400
# server_id -> когда выбран путь через ретранслятор: helper откроет порт на следующем обращении
_VIA: dict[int, float] = {}

# скрипт пишет файл на каждом прогоне (крон раз в 2 минуты): старше - он не запускается
STALE_AFTER = 600


def note(ip: str, what: str, now: float | None = None) -> None:
    """Нода что-то скачала с панели: what = script (установщик) или agent (бинарь)."""
    ip = (ip or "").strip()
    if not ip:
        return
    now = time.time() if now is None else now
    _SEEN.setdefault(ip, {})[what] = now
    if len(_SEEN) > _SEEN_MAX:
        for k in [k for k, v in _SEEN.items() if now - max(v.values()) > _SEEN_TTL]:
            del _SEEN[k]
        while len(_SEEN) > _SEEN_MAX:  # все свежие: выкидываем самые старые
            del _SEEN[min(_SEEN, key=lambda k: max(_SEEN[k].values()))]


def seen(ip: str, since: float = 0) -> dict[str, float]:
    """Что нода с этого адреса скачала не раньше since (создание сервера)."""
    return {k: v for k, v in (_SEEN.get((ip or "").strip()) or {}).items() if v >= since}


def via_relay(server_id: int, now: float | None = None) -> None:
    _VIA[server_id] = time.time() if now is None else now


def relay_state(server_id: int, relay_seen: float | None) -> str:
    """open - helper ретранслятора обращался к панели после выбора пути (и сразу открыл порт),
    opening - еще нет, '' - не знаем (путь выбран до перезапуска панели)."""
    at = _VIA.get(server_id)
    if at is None:
        return ""
    return "open" if relay_seen is not None and relay_seen > at else "opening"


def read_applied(data_dir: str) -> tuple[str, float, list[str]] | None:
    """(фаервол, когда скрипт отработал, открытые адреса) или None, если отчета нет."""
    path = os.path.join(data_dir, "agent_allow_ips.applied")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read(1 << 20).splitlines()
        mtime = os.path.getmtime(path)
    except (OSError, UnicodeDecodeError):
        return None
    fw, ts = "", mtime
    head = lines[0] if lines else ""
    if head.startswith("#"):
        for part in head.split():
            if part.startswith("fw="):
                fw = part[3:]
            elif part.startswith("ts="):
                try:
                    ts = float(part[3:])
                except ValueError:
                    pass
    ips = [x.strip() for x in lines if x.strip() and not x.startswith("#")]
    return fw, ts, ips


def _covered(ip: str, opened: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in opened:
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def firewall_state(ip: str, data_dir: str, now: float | None = None) -> tuple[str, int | None]:
    """Адрес ноды в фаерволе панели: (состояние, секунд с прогона скрипта).

    '' - сказать нечего (скрипта нет или фаервола нет), open - открыт, opening - скрипт его еще
    не открыл, no_ip - фаервол есть, а IP сервера не указан (открывать нечего), stale - скрипт
    давно не запускался, сам адрес не откроется."""
    rep = read_applied(data_dir)
    if rep is None:
        return "", None
    fw, ts, opened = rep
    now = time.time() if now is None else now
    age = max(0, int(now - ts))
    if fw == "none":
        return "", age
    ip = (ip or "").strip()
    if not ip:
        return "no_ip", age
    if _covered(ip, opened):
        return "open", age
    return ("stale" if age > STALE_AFTER else "opening"), age
