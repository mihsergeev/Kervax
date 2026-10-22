"""Куда идет бэкап ноды и по какому каналу.

helper backup-setup отдает repo_dest - адрес репозитория restic из env бэкапа, логин и пароль он
вырезает сам (rest:http://host:64100/name). Панель повторяет вырезку на приеме отчета (clean): в
базу, логи и API адрес попадает только без логина и пароля, даже от старого или чужого helper'а.

Бэкап по rest:http идет через интернет открытым текстом: логин и пароль rest-server (Basic auth)
видны любому на пути (содержимое restic шифрует сам, но с этим логином репозиторий открыт чужому).
Это не авария, а дело (пункт "Требует действий"), алерта в Telegram нет: на http ходит почти весь
парк, сообщение на каждый сервер было бы спамом, а само по себе на http ничего не переключается -
только человек, который переустанавливает бэкап. Внутренние адреса (частные сети, CGNAT меша,
loopback) не флагуем: там канал не идет через интернет. Решение "по http оставить" - в alert_mutes
(MUTE_KEY), пункт уходит из "Требует действий", в окне бэкапа виден приглушенным с "вернуть".
Имя сервера бэкапов для показа ищет фронт по списку серверов; для перевода на https - бэкенд
(find_backup_server), он же проверяет версии агента и helper'ов (https_blockers).
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from app import open_ports

MUTE_KEY = "bkhttp:repo"
# схемы restic, которые сами идут с шифрованием: sftp по ssh, облака по https
_TLS = {"https", "sftp", "s3", "b2", "azure", "gs", "swift", "rclone"}
_DEFAULT_PORT = {"http": 80, "https": 443, "sftp": 22}
# userinfo (логин:пароль@) в любом URL-подобном адресе
_USERINFO = re.compile(r"(://)[^/@\s]*@")
# sftp:user@host:/path - логин без схемы с //
_SFTP_USER = re.compile(r"^(sftp:)[^@/\s]+@")


def clean(dest: str) -> str:
    """Адрес репозитория без логина и пароля. Что угодно не строкой - пустая строка."""
    if not isinstance(dest, str):
        return ""
    s = dest.strip()[:500]
    s = _USERINFO.sub(r"\1", s)
    return _SFTP_USER.sub(r"\1", s)


def clean_report(rep: dict) -> None:
    """Вырезает логин и пароль из backup.repo_dest отчета агента (на месте)."""
    b = rep.get("backup") if isinstance(rep, dict) else None
    if isinstance(b, dict) and b.get("repo_dest"):
        b["repo_dest"] = clean(b["repo_dest"])


def parse(dest: str) -> dict | None:
    """{"kind", "proto", "host", "port", "repo"} из адреса repo_dest, None - адреса нет или он не
    разбирается. kind - бэкенд restic (rest, s3, sftp, local...), proto - канал (http, https, sftp...)."""
    s = clean(dest)
    if not s:
        return None
    if s.startswith("/"):
        return {"kind": "local", "proto": "local", "host": "", "port": None, "repo": s}
    kind, _, rest = s.partition(":")
    kind = kind.lower()
    if kind == "sftp":
        # sftp:host:/path или sftp://host:port/path
        if rest.startswith("//"):
            u = urlsplit("sftp:" + rest)
            return {"kind": "sftp", "proto": "sftp", "host": u.hostname or "", "port": _port(u) or 22,
                    "repo": u.path.strip("/")}
        host, _, path = rest.partition(":")
        return {"kind": "sftp", "proto": "sftp", "host": host, "port": 22, "repo": path.strip("/")}
    if kind in ("rest", "s3") and rest.startswith(("http://", "https://")):
        u = urlsplit(rest)
        proto = u.scheme
        return {"kind": kind, "proto": proto, "host": u.hostname or "", "port": _port(u) or _DEFAULT_PORT[proto],
                "repo": u.path.strip("/")}
    if kind == "s3":
        # s3:host/bucket - restic ходит по https
        host, _, path = rest.partition("/")
        return {"kind": "s3", "proto": "https", "host": host, "port": 443, "repo": path.strip("/")}
    if kind in _TLS:
        return {"kind": kind, "proto": "https", "host": "", "port": None, "repo": rest}
    return None


def _port(u) -> int | None:
    try:
        return u.port
    except ValueError:
        return None


def _internal(host: str) -> bool:
    """Адрес, до которого канал не идет через интернет: loopback, частные сети, CGNAT меша."""
    if host.lower() in ("localhost", ""):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False  # имя хоста: где оно, не знаем, считаем внешним
    return not open_ports.is_public(host)


def view(rep: dict, mutes: list[str] | None = None) -> dict | None:
    """Куда идет бэкап ноды: разобранный адрес, insecure - открытым текстом через интернет (пункт
    "Требует действий"), muted - решение "по http оставить"."""
    b = (rep or {}).get("backup") or {}
    t = parse(b.get("repo_dest") or "")
    if t is None:
        return None
    t["insecure"] = t["proto"] == "http" and not _internal(t["host"])
    t["muted"] = MUTE_KEY in (mutes or [])
    return t


# Перевод на https (кнопка в окне бэкапа и пачкой в окне сервера бэкапов). Клиентский helper сам
# собирает https-адрес из своего env, читает репозиторий по https с сертификатом сервера бэкапов
# и переключается только если это получилось; сервер бэкапов открывает клиенту TLS-порт
AGENT_MIN = (2, 48)          # агент знает действия to_https и allow_tls
CLIENT_HELPER_MIN = (0, 40)  # backup-setup: to-https
BSRV_HELPER_MIN = (0, 35)    # backupserver-setup: allow-tls


def _vt(v: object) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:2])


def _helper(srv, name: str) -> tuple[int, ...]:
    return _vt(((srv.last_report or {}).get("setup_versions") or {}).get(name))


def addrs(srv) -> set[str]:
    """Адреса и имена сервера, по которым к нему может ходить клиент бэкапа."""
    rep = srv.last_report or {}
    out = {srv.external_ip, srv.agent_ip, srv.local_ip, srv.hostname, srv.name, *(rep.get("ips") or [])}
    return {str(x).lower() for x in out if x}


def https_blockers(client, bs) -> list[str]:
    """Почему клиента сейчас не перевести на https через этот сервер бэкапов; пусто - можно."""
    t = view(client.last_report or {})
    if not t or t["kind"] != "rest":
        return ["бэкап настроен не панелью или не через rest-server"]
    if t["proto"] != "http":
        return ["бэкап уже идет по https"]
    if bs is None or t["host"].lower() not in addrs(bs):
        return [f"сервер бэкапов {t['host']} не найден в панели"]
    out = []
    if not ((bs.last_report or {}).get("backup_server") or {}).get("present"):
        out.append(f"{bs.name}: панель не видит на нем rest-server")
    for srv, name, need in ((client, "backup-setup", CLIENT_HELPER_MIN), (bs, "backupserver-setup", BSRV_HELPER_MIN)):
        if _vt(srv.agent_version) < AGENT_MIN:
            out.append(f"{srv.name}: нужен агент {AGENT_MIN[0]}.{AGENT_MIN[1]} или новее")
        if _helper(srv, name) < need:
            out.append(f"{srv.name}: нужен helper {name} {need[0]}.{need[1]} или новее")
    return list(dict.fromkeys(out))


def find_backup_server(client, servers):
    """Сервер бэкапов клиента среди серверов панели: хост из адреса репозитория - его адрес или имя.
    Сначала те, где панель видит rest-server."""
    t = view(client.last_report or {})
    if not t or not t["host"]:
        return None
    hits = [s for s in servers if s.id != client.id and t["host"].lower() in addrs(s)]
    hits.sort(key=lambda s: not ((s.last_report or {}).get("backup_server") or {}).get("present"))
    return hits[0] if hits else None


def https_view(client, servers) -> dict | None:
    """Для бэкапа по http: на какой сервер бэкапов переводить и что мешает ({bs_id, blockers})."""
    t = view(client.last_report or {})
    if not t or t["kind"] != "rest" or t["proto"] != "http":
        return None
    bs = find_backup_server(client, servers)
    return {"bs_id": bs.id if bs else None, "blockers": https_blockers(client, bs)}


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
