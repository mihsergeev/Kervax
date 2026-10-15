"""Пробная проверка нового монитора - до того, как его сохранили.

Форма "Добавить монитор" заводила монитор вслепую: откроется ли адрес, человек узнавал от
красного монитора и алерта, а "откуда проверять" выбирал наугад между тремя переключателями
без подсказки. Теперь кнопка "Проверить" открывает адрес так, как его потом будет проверять
сам монитор:
  - с панели: каждый адрес домена отдельно, плюс срок сертификата и домена;
  - из каждой локации с прокси;
  - изнутри сервера, чей веб-сервер держит этот домен. Задание агенту идет тем же путем,
    что "Проверить сейчас" у локального монитора (manual_probe), с настройками из формы.
Форма показывает итог каждого способа и предлагает рабочий.

Ничего не сохраняется, кроме задания агенту. Пароль и заголовки стираются из него, как
только агент ответил, а сами задания вычищает общий ретеншен probe_requests.

Части проверки форма запрашивает отдельно и одновременно: ответ панели приходит за доли
секунды, а прокси далекой локации или регистратура домена могут думать десяток секунд, и
ждать самого медленного, глядя на крутилку у готового результата, незачем.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import checks as checks_exec
from app import domain_probe, manual_probe
from app.collector import _aware, seen_online
from app.config import get_settings
from app.models import Location, ProbeRequest, Server

KIND = "try"


def check_of(body) -> SimpleNamespace:
    """Монитор из формы - такой, каким его заведут, но одна попытка вместо повторов:
    это взгляд перед сохранением, а не вердикт для алерта."""
    chk = manual_probe.domain_check(body.target.strip())
    for key in manual_probe.SPEC_FIELDS:
        setattr(chk, key, getattr(body, key))
    chk.type = body.type
    chk.port = body.port
    chk.degraded_ms = body.degraded_ms
    chk.extra_paths = list(body.extra_paths or [])
    chk.retries = 0
    chk.check_ssl = True
    chk.check_domain = True
    # адреса домена смотрим всегда: мертвый адрес за балансировщиком иначе не увидеть,
    # а включить "проверять все адреса" форма предложит сама
    chk.check_all_ips = body.type == "http"
    return chk


def result(outcome: checks_exec.CheckOutcome) -> dict:
    kind, code = domain_probe._kind(outcome.status, outcome.message)
    return {
        "status": outcome.status, "latency_ms": outcome.latency_ms,
        "message": outcome.message[:512], "kind": kind, "code": code,
    }


async def panel(chk) -> dict:
    try:
        outcome = await checks_exec.run_check(chk)
    except Exception as exc:  # noqa: BLE001 - проверка не должна ронять ответ
        outcome = checks_exec.CheckOutcome(
            "down", message=checks_exec.humanize_error(str(exc) or type(exc).__name__))
    out = result(outcome)
    out["ip_results"] = outcome.ip_results
    out["path_results"] = outcome.path_results
    return out


async def expiry(chk) -> dict:
    info = await checks_exec.probe_expiry(chk)
    return {
        "ssl_days": info.ssl_days, "ssl_message": info.ssl_message[:256],
        "domain_days": info.domain_days, "domain_message": info.domain_message[:256],
    }


async def locations(session: AsyncSession, chk) -> list[dict]:
    """Каждая локация с прокси - одной попыткой, с теми же послаблениями на даль, что у
    монитора. "Напрямую" не гоняем: это та же проверка с панели, второй строкой."""
    locs = [
        loc for loc in await session.scalars(
            select(Location).where(Location.enabled.is_(True), Location.monitors.is_(True))
            .order_by(Location.name)
        )
        if (loc.url or "").strip()
    ]
    s = get_settings()

    async def one(loc: Location) -> dict:
        try:
            outcome = await checks_exec._run_http(
                chk, proxy=loc.url.strip(),
                degraded_ms=chk.degraded_ms + max(s.location_degraded_extra_ms, 0),
                timeout_ms=chk.timeout_ms + max(s.location_timeout_extra_ms, 0),
            )
        except Exception as exc:  # noqa: BLE001
            outcome = checks_exec.CheckOutcome(
                "down", message=checks_exec.humanize_error(str(exc) or type(exc).__name__))
        return {"id": loc.id, "name": loc.name, **result(outcome)}

    return list(await asyncio.gather(*(one(loc) for loc in locs)))


def host_of(target: str) -> str:
    t = (target or "").strip().lower()
    if "://" not in t:
        t = "http://" + t
    return (urlparse(t).hostname or "").strip(".")


def serving_web(servers: list[Server], host: str) -> tuple[Server, str] | None:
    """Сервер, чей веб-сервер держит домен, и что это за веб-сервер (для подписи "домен
    есть в nginx"). Несколько нод - первая по имени, как у привязки локального монитора."""
    for srv in sorted(servers, key=lambda x: x.name):
        for web in (srv.last_report or {}).get("web_services") or []:
            for raw in web.get("sites") or []:
                d = (raw or "").strip().lower().rstrip(".")
                d = d.split("://", 1)[-1].split("/")[0].split(":", 1)[0]
                if d and d == host:
                    return srv, str(web.get("kind") or "")
    return None


async def inside(session: AsyncSession, body, servers: list[Server], now: datetime) -> dict:
    """Задание агенту сервера сайта. Ответ форма забирает опросом (poll)."""
    host = host_of(body.target)
    hit = serving_web(servers, host) if host else None
    if hit is None:
        return {"status": "none", "kind": "no_server"}
    srv, web = hit
    base = {"server_id": srv.id, "server_name": srv.name, "web": web}
    if not seen_online(srv, now):
        return {**base, "status": "none", "kind": "offline"}
    req = ProbeRequest(
        check_id=0, server_id=srv.id, url=body.target.strip(), kind=KIND, created_at=now,
        spec={k: getattr(body, k) for k in manual_probe.SPEC_FIELDS},
    )
    session.add(req)
    srv.probe_pending_at = now
    await session.flush()  # нужен номер запроса: по нему придет ответ агента
    return {**base, "status": "pending", "request_id": req.id,
            "fast": manual_probe.fast_agent(srv)}


def inside_answer(req: ProbeRequest, srv: Server | None, now: datetime) -> dict:
    """Итог задания: ждем, готово или агент не ответил вовремя."""
    hit = serving_web([srv], host_of(req.url)) if srv is not None else None
    base = {"server_id": req.server_id, "server_name": srv.name if srv else "",
            "web": hit[1] if hit else "", "request_id": req.id,
            "fast": manual_probe.fast_agent(srv)}
    if req.ts is not None and req.status:
        kind, code = domain_probe._kind(req.status, req.message)
        return {**base, "status": req.status, "message": req.message,
                "latency_ms": None if req.error else req.latency_ms, "kind": kind, "code": code}
    deadline = manual_probe.deadline_seconds(manual_probe.request_check(req))
    if (now - _aware(req.created_at)).total_seconds() > deadline:
        manual_probe.drop_secrets(req)
        return {**base, "status": "none", "kind": "no_answer", "deadline": deadline}
    return {**base, "status": "pending"}
