"""Журнал ошибок самой панели: на что человек натыкается в интерфейсе, без скриншотов.

Повод: человек нажал в карточке "включить дамп", под строкой красным "пробный дамп не прошел:
-", и единственное, что можно было сделать, - прислать скриншот. Здесь это собирается само:

  * необработанное исключение бэкенда (500) - маршрут, кто, короткий traceback (middleware);
  * отказ на изменяющий запрос человека (POST/PUT/PATCH/DELETE с 4xx/5xx), кроме ожидаемого
    шума: 401 (протух вход, его лечит повторный вход), 422 (форма не прошла проверку) и любых
    отказов /api/auth/ (неверный пароль или код - у входа своя защита и свой журнал);
  * провал команды агенту или helper'у, которую поставил человек (Docker, Kubernetes, бэкап,
    юниты, место на диске), с выводом helper'а и параметрами команды;
  * ошибка интерфейса (window.onerror, unhandledrejection, error boundary) - с фронта, с
    ограничением частоты на обеих сторонах;
  * сообщение "сообщить о проблеме": пара слов от человека, к ним сами прикладываются его
    ошибки за последние RECENT_MIN минут.

Одинаковая ошибка - одна строка: счетчик и время последней (fp - отпечаток без чисел и id).
Разобранная и повторившаяся - снова неразобранная, с пометкой reopened. Хранение - KEEP и не
больше MAX_ROWS строк (collector._prune). Тела запросов целиком не хранятся: первые
_BODY_KEPT байт, поля вроде password/token/secret/key замаскированы, как и похожие на секреты
куски текста (Bearer, user:pass@ в адресе, токен бота, приватный ключ).

В Telegram журнал не шлется: наши собственные поломки - не повод будить дежурного. Админ
видит счетчик неразобранных у шестеренки и окно "Ошибки панели".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sysconfig
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.impersonate import READ_ONLY, who_of
from app.models import PanelError, Server

log = logging.getLogger(__name__)

KEEP = timedelta(days=30)
MAX_ROWS = 5000
RECENT_MIN = 15  # ошибки человека за столько минут прикладываются к его сообщению
KINDS = ("server", "api", "command", "front", "report")

# изменяющие запросы человека: отказ на них - "действие не удалось"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# запросы машин (агенты, push-скрипты, ansible): их отказы считает app/request_watch.py, сюда -
# только исключения бэкенда
MACHINE = ("/api/agent/", "/api/ansible/")
SKIP = frozenset({"/api/health"})
# ожидаемые отказы: протухший вход и форма, не прошедшая проверку
NOISY_CODES = frozenset({401, 422})
QUIET_PREFIX = ("/api/auth/", "/api/panel-errors")

# начало ответа на изменение во время входа под чужой учеткой (impersonate.READ_ONLY)
_IMP_READ_ONLY = READ_ONLY.split("{")[0].encode()

_MSG_MAX = 2000
_DETAIL_MAX = 8000
_BODY_KEPT = 4096
_TB_LINES = 30
# одинаковая ошибка чаще раза в _THROTTLE_S из процесса - только счетчик в памяти, без записи
_THROTTLE_S = 5.0

_tasks: set[asyncio.Task] = set()
_recent: dict[str, list[float]] = {}  # fp -> [время последней записи, сколько накопилось]

# --- секреты ----------------------------------------------------------------------------------

_KEY_WORD = r"(?:pass|secret|token|api_?key|apikey|private_?key|access_?key|creds?|credential|otp|cookie|auth|session|signature)"
SENSITIVE_KEY = re.compile(rf"{_KEY_WORD}|^key$|_key$", re.I)
_MASK_RES = (
    # приватный ключ целиком
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 ***"),
    # пароль в адресе: scheme://user:pass@host
    (re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+):[^@\s/]+@", re.I), r"\1:***@"),
    # токен Telegram-бота
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "***"),
    # key=value и "key": "value", где имя похоже на секрет (REPOPASS_B64=..., "password": "...")
    (re.compile(
        rf"""(?i)(["']?[A-Za-z0-9_.-]*{_KEY_WORD}[A-Za-z0-9_.-]*["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;&}}\]]+)"""),
     r"\1***"),
)


def mask_text(s: Any, limit: int = _MSG_MAX) -> str:
    out = "" if s is None else str(s)
    for rx, rep in _MASK_RES:
        out = rx.sub(rep, out)
    if len(out) > limit:
        out = out[:limit] + " ..."
    return out


def mask_obj(obj: Any, depth: int = 0) -> Any:
    """Тело запроса для журнала: значения полей-секретов скрыты, строки обрезаны, вложенность и
    длина списков ограничены."""
    if depth > 4:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= 40:
                out["..."] = f"{len(obj) - 40} more"
                break
            out[str(k)[:60]] = "***" if SENSITIVE_KEY.search(str(k)) and v not in (None, "", [], {}) else mask_obj(v, depth + 1)
        return out
    if isinstance(obj, list):
        items = [mask_obj(v, depth + 1) for v in obj[:20]]
        if len(obj) > 20:
            items.append(f"... {len(obj) - 20} more")
        return items
    if isinstance(obj, str):
        return mask_text(obj, 300)
    return obj


def body_summary(raw: bytes, content_type: str) -> str:
    """Начало тела запроса: JSON - разобранным и с маской, остальное - только размер."""
    if not raw:
        return ""
    if "json" in (content_type or ""):
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return mask_text(raw[:300].decode("utf-8", "replace"), 300) + (" ..." if len(raw) > 300 else "")
        return json.dumps(mask_obj(data), ensure_ascii=False)[:1500]
    return f"<{content_type or 'body'}, {len(raw)} B>"


# --- отпечаток ----------------------------------------------------------------------------------

_NUMS = re.compile(r"\b[0-9a-f]{8,}\b|\d+", re.I)


def fingerprint(kind: str, action: str, message: str, status: int | None = None, extra: str = "") -> str:
    """Одинаковая ошибка - один отпечаток: числа, id и хеши в тексте не различают ошибки."""
    norm = _NUMS.sub("#", (message or "").strip().lower())[:300]
    raw = "\x1f".join((kind, action or "", str(status or ""), norm, extra))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def short_tb(exc: BaseException) -> str:
    """Короткий traceback: кадры кода панели (без библиотек и asyncio) и сама ошибка. Если кадров
    панели нет - последние три кадра как есть."""
    frames = traceback.extract_tb(exc.__traceback__)
    std = os.path.normcase(sysconfig.get_paths().get("stdlib") or "\0")
    mine = [f for f in frames if "site-packages" not in f.filename
            and not os.path.normcase(f.filename).startswith(std)]
    shown = (mine or frames[-3:])[-_TB_LINES // 2:]
    text = "Traceback (код панели):\n" + "".join(traceback.format_list(shown))
    text += "".join(traceback.format_exception_only(type(exc), exc))
    return mask_text(text, _DETAIL_MAX)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _version() -> str:
    try:
        return get_settings().version
    except Exception:  # noqa: BLE001
        return ""


# --- запись -------------------------------------------------------------------------------------

async def record(
    session_factory,
    *,
    kind: str,
    action: str = "",
    message: str = "",
    detail: str = "",
    status: int | None = None,
    username: str = "",
    page: str = "",
    section: str = "",
    server_id: int | None = None,
    server_name: str = "",
    extra: dict | None = None,
    fp_extra: str = "",
    dedupe: bool = True,
) -> int | None:
    """Записать ошибку или прибавить счетчик у такой же. Никогда не бросает: журнал не должен
    ронять запрос, который и так закончился ошибкой. Возвращает id строки (None - не записано)."""
    if session_factory is None:
        return None
    try:
        message = mask_text(message, _MSG_MAX)
        detail = mask_text(detail, _DETAIL_MAX)
        if kind == "command" and server_id is not None:
            fp_extra = f"{fp_extra}|srv={server_id}"
        fp = fingerprint(kind, action, message, status, fp_extra) if dedupe else hashlib.sha1(
            f"{kind}|{time.time_ns()}|{id(extra)}".encode()).hexdigest()
        add = 1
        if dedupe:
            mono = time.monotonic()
            cur = _recent.get(fp)
            if cur is not None and mono - cur[0] < _THROTTLE_S:
                cur[1] += 1
                return None
            if cur is not None:
                add += int(cur[1])
            _recent[fp] = [mono, 0]
            if len(_recent) > 2000:
                _recent.clear()
        now = _now()
        async with session_factory() as s:
            if server_id is not None and not server_name:
                srv = await s.get(Server, server_id)
                server_name = (srv.name or "") if srv is not None else ""
            row = None
            if dedupe:
                row = await s.scalar(select(PanelError).where(PanelError.fp == fp).order_by(PanelError.id.desc()).limit(1))
            if row is None:
                row = PanelError(fp=fp, kind=kind, first_at=now, count=add)
                s.add(row)
            elif row.resolved:
                # разобрали, а оно снова: заново неразобранная, счет с нуля
                row.resolved, row.resolved_at, row.resolved_by, row.reopened = False, None, "", True
                row.first_at, row.count = now, add
            else:
                row.count = (row.count or 0) + add
            row.last_at = now
            row.action = (action or "")[:200]
            row.message = message
            row.detail = detail
            row.status = status
            row.username = (username or row.username or "")[:64]
            row.page = (page or "")[:300]
            row.section = (section or "")[:32]
            row.server_id = server_id
            row.server_name = (server_name or "")[:200]
            row.version = _version()[:32]
            row.extra = extra
            await s.commit()
            return row.id
    except Exception:  # noqa: BLE001
        log.warning("ошибка панели не записана в журнал", exc_info=True)
        return None


def record_soon(session_factory, **fields) -> None:
    """record в фоне: ответ человеку не ждет записи (база может как раз и лежать)."""
    try:
        t = asyncio.get_running_loop().create_task(record(session_factory, **fields))
    except RuntimeError:
        return
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Дождаться фоновых записей (тесты)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset() -> None:
    _recent.clear()


# --- команды агенту -----------------------------------------------------------------------------

_CMD_SECTION = {"docker": "docker", "kube": "kuber", "backup": "backups"}
_CMD_PARAMS = ("container", "ns", "kind", "name", "mode", "schedule", "tail", "since")


def command_params(kind: str, c) -> dict:
    out: dict = {}
    for f in _CMD_PARAMS:
        v = getattr(c, f, None)
        if v not in (None, "", 0):
            out[f] = v
    paths = getattr(c, "paths", None)
    if paths:
        out["paths"] = list(paths)[:10] + ([f"... {len(paths) - 10} more"] if len(paths) > 10 else [])
    payload = getattr(c, "payload", None)
    if isinstance(payload, dict) and payload:
        out["payload"] = payload
    return mask_obj(out)


def command_failed(session_factory, kind: str, c, *, server_name: str = "", soon: bool = True):
    """Команда, поставленная человеком, не удалась (агент ответил ошибкой или не ответил).
    Команды планировщика (created_by пусто) сюда не идут: у них свои уведомления."""
    who = getattr(c, "created_by", "") or ""
    if not who or getattr(c, "origin", "") == "auto":
        return None
    action = getattr(c, "action", "") or ""
    if kind == "backup" and action in ("unit_fix", "unit_journal"):
        section = "servers"
    else:
        section = _CMD_SECTION.get(kind, "")
    out = (getattr(c, "result", "") or "").strip()
    first = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "") if out else ""
    fields = dict(
        kind="command", action=f"{kind} {action}".strip(), message=first or "команда не удалась, вывода нет",
        detail=out[-_DETAIL_MAX:], username=who, section=section,
        server_id=getattr(c, "server_id", None), server_name=server_name,
        extra={"cmd_id": getattr(c, "id", None), "params": command_params(kind, c)},
    )
    if soon:
        record_soon(session_factory, **fields)
        return None
    return record(session_factory, **fields)


# --- middleware --------------------------------------------------------------------------------

_SRV_IN_PATH = re.compile(r"^/api/servers/(\d+)(?:/|$)")


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1", "replace")
    return ""


def who_from_scope(scope) -> str:
    auth = _header(scope, b"authorization")
    if not auth.lower().startswith("bearer "):
        return ""
    from app.security import decode_access_token

    try:
        payload = decode_access_token(auth[7:].strip(), get_settings().jwt_secret)
    except Exception:  # noqa: BLE001
        return ""
    # вход админа под чужой учеткой: "админ как пользователь", ошибку поймал админ
    return who_of(payload)


def _route(scope, path: str) -> str:
    """Шаблон маршрута (/api/servers/{server_id}/backup/command): одинаковые запросы к разным id
    складываются. Шаблон у роутера - без префикса подключения (/servers/...), его добираем из
    самого адреса по числу частей."""
    r = scope.get("route")
    tpl = getattr(r, "path", None)
    if isinstance(tpl, str) and tpl.startswith("/"):
        if path.startswith(tpl.split("{")[0]) and tpl.count("/") == path.rstrip("/").count("/"):
            return tpl
        cut = path.rstrip("/").count("/") - tpl.count("/")
        if cut > 0:
            return "/" + "/".join(path.strip("/").split("/")[:cut]) + tpl
    from app.request_watch import norm_path

    return norm_path(path)


def _detail_of(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw[:500].decode("utf-8", "replace")
    d = data.get("detail") if isinstance(data, dict) else data
    if isinstance(d, str):
        return d
    return json.dumps(mask_obj(d), ensure_ascii=False)[:1000]


def should_record(method: str, path: str, status: int) -> str | None:
    """Вид записи для ответа без исключения: server, api или None (не пишем)."""
    if path in SKIP or not path.startswith("/api/"):
        return None
    machine = path.startswith(MACHINE)
    if status >= 500:
        if machine and status != 500:
            return None
        return "server"
    if status < 400 or machine or method not in WRITE_METHODS:
        return None
    if status in NOISY_CODES or path.startswith(QUIET_PREFIX):
        return None
    return "api"


class PanelErrorMiddleware:
    """Ловит исключения бэкенда и неуспешные изменяющие запросы человека (should_record)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/") or path in SKIP:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        keep_body = method in WRITE_METHODS and not path.startswith(MACHINE) and not path.startswith(QUIET_PREFIX)
        req = bytearray()
        resp = bytearray()
        code = 0

        async def _receive():
            msg = await receive()
            if keep_body and msg.get("type") == "http.request" and len(req) < _BODY_KEPT:
                req.extend(msg.get("body", b"")[: _BODY_KEPT - len(req)])
            return msg

        async def _send(msg) -> None:
            nonlocal code
            if msg["type"] == "http.response.start":
                code = msg["status"]
            elif msg["type"] == "http.response.body" and code >= 400 and len(resp) < _BODY_KEPT:
                resp.extend(msg.get("body", b"")[: _BODY_KEPT - len(resp)])
            await send(msg)

        try:
            await self.app(scope, _receive, _send)
        except Exception as exc:
            self._note(scope, path, method, 500, req, b"", exc)
            raise
        kind = should_record(method, path, code)
        # отказ в изменении, пока админ смотрит панель глазами пользователя (app/impersonate.py), -
        # ожидаемый, как и протухший вход: не ошибка панели
        if kind == "api" and code == 403 and _IMP_READ_ONLY in resp:
            kind = None
        if kind:
            self._note(scope, path, method, code, req, bytes(resp), None)

    def _note(self, scope, path, method, code, req, resp, exc) -> None:
        try:
            factory = getattr(getattr(scope.get("app"), "state", None), "session_factory", None)
            route = _route(scope, path)
            m = _SRV_IN_PATH.match(path)
            srv = int(m.group(1)) if m else None
            body = body_summary(bytes(req), _header(scope, b"content-type")) if req else ""
            if exc is not None:
                kind, message, detail = "server", f"{type(exc).__name__}: {exc}", short_tb(exc)
            else:
                kind = "server" if code >= 500 else "api"
                message, detail = _detail_of(resp) or f"HTTP {code}", ""
            if body:
                detail = (detail + "\n\n" if detail else "") + f"тело запроса: {body}"
            record_soon(
                factory, kind=kind, action=f"{method} {route}", message=message, detail=detail,
                status=code, username="" if path.startswith(MACHINE) else who_from_scope(scope),
                page=_header(scope, b"x-kervax-page")[:300], section=_header(scope, b"x-kervax-section")[:32],
                server_id=srv,
            )
        except Exception:  # noqa: BLE001
            log.debug("журнал ошибок панели", exc_info=True)


# --- чтение -------------------------------------------------------------------------------------

def row_out(r: PanelError) -> dict:
    def iso(ts):
        if ts is None:
            return None
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()

    return {
        "id": r.id, "kind": r.kind, "first_at": iso(r.first_at), "last_at": iso(r.last_at),
        "count": r.count or 1, "username": r.username or "", "page": r.page or "",
        "section": r.section or "", "server_id": r.server_id, "server_name": r.server_name or "",
        "action": r.action or "", "status": r.status, "message": r.message or "",
        "detail": r.detail or "", "version": r.version or "", "extra": r.extra or {},
        "resolved": bool(r.resolved), "resolved_at": iso(r.resolved_at),
        "resolved_by": r.resolved_by or "", "reopened": bool(r.reopened),
    }


def short_out(r: PanelError) -> dict:
    """Строка для вложения в сообщение человека: без вывода и тела запроса."""
    d = row_out(r)
    return {k: d[k] for k in ("id", "kind", "last_at", "count", "action", "status", "message", "server_name", "page")}


async def recent_for(session, username: str, minutes: int = RECENT_MIN) -> list[PanelError]:
    since = _now() - timedelta(minutes=minutes)
    return list(await session.scalars(
        select(PanelError).where(PanelError.username == username, PanelError.kind != "report",
                                 PanelError.last_at >= since)
        .order_by(PanelError.last_at.desc()).limit(20)))


async def prune(session, now: datetime | None = None) -> None:
    """Срок KEEP и потолок MAX_ROWS (лишнее - самое старое по last_at). Без commit."""
    now = now or _now()
    await session.execute(delete(PanelError).where(PanelError.last_at < now - KEEP))
    total = await session.scalar(select(func.count()).select_from(PanelError)) or 0
    if total > MAX_ROWS:
        edge = await session.scalar(
            select(PanelError.last_at).order_by(PanelError.last_at.desc()).offset(MAX_ROWS - 1).limit(1))
        if edge is not None:
            await session.execute(delete(PanelError).where(PanelError.last_at < edge))
