import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from sqlalchemy import func, or_, select, update

from app import alerts, audit, collector, heartbeat, notification_store, panel_errors, settings_store, tg_bot
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.models import AuditLog, Check, NotificationDelivery, NotificationEvent, Server, User
from app.schemas import (
    MuteWarning,
    AlertCoverageOut,
    CoverageFixIn,
    CoverageFixOut,
    AlertConfigIn,
    AlertConfigOut,
    AlertTestResult,
    DupPauseIn,
    RouteTestIn,
    ServerAlertKindInfo,
    ServerAlertRulesIn,
    ServerAlertRulesOut,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])

_DELIVERY_PROBLEMS = ("attention", "rejected", "uncertain")
_DELIVERY_STATUSES = ("pending", "sending", "confirmed", "uncertain", "rejected", "superseded", "attention")
_RETRY_WARNING = "Предыдущая доставка не подтверждена, повтор может прислать копию"


class DeliveryIdsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]] = Field(min_length=1, max_length=50)


class DeliveryRetryIn(DeliveryIdsIn):
    acknowledge_uncertain: bool = False


def _delivery_time(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


@router.get("/deliveries")
async def get_deliveries(_: AdminUser, session: SessionDep,
                         state: Literal["problems", "all", "reviewed"] = "problems", summary: bool = False) -> dict:
    """Admin-only safe metadata; no payloads, recipient addresses or credentials."""
    delivery_model = NotificationDelivery
    problem = delivery_model.status.in_(_DELIVERY_PROBLEMS) & or_(
        delivery_model.status != "attention", delivery_model.reviewed_at.is_(None))
    counts = dict.fromkeys(_DELIVERY_STATUSES, 0)
    totals = (await session.execute(select(delivery_model.status, func.count(), func.count().filter(problem))
                                    .group_by(delivery_model.status))).all()
    counts.update({name: number for name, number, _ in totals})
    query = select(NotificationDelivery, NotificationEvent).join(
        NotificationEvent, NotificationEvent.id == NotificationDelivery.event_id)
    if state == "problems":
        query = query.where(problem)
    if state == "reviewed":
        query = query.where(delivery_model.reviewed_at.is_not(None)).order_by(delivery_model.reviewed_at.desc())
    rows = [] if summary else (await session.execute(query.order_by(delivery_model.id.desc()).limit(51))).all()
    items = []
    for delivery, event in rows[:50]:
        message = (event.payload or {}).get("message") or {}
        items.append({
            "id": delivery.id, "event_id": event.id,
            "kind": panel_errors.mask_text(message.get("kind") or "", 40),
            "target": panel_errors.mask_text(message.get("target") or "", 255),
            "phase": event.phase, "status": delivery.status,
            "attempts": delivery.attempts, "uncertain_count": delivery.uncertain_count,
            "created_at": _delivery_time(delivery.created_at),
            "occurred_at": _delivery_time(event.occurred_at),
            "next_attempt_at": _delivery_time(delivery.next_attempt_at),
            "lease_until": _delivery_time(delivery.lease_until),
            "reviewed_at": _delivery_time(delivery.reviewed_at), "reviewed_by": delivery.reviewed_by,
            "can_review": delivery.status == "attention" and delivery.reviewed_at is None
            and delivery.lease_until is None and not delivery.lease_owner,
            "can_retry": delivery.status in _DELIVERY_PROBLEMS and delivery.reviewed_at is None,
            "needs_confirmation": delivery.uncertain_count > 0 or delivery.status == "uncertain",
            # код причины последнего сбоя (PrepareError, LeaseExpired...), без текста исключения
            "reason": notification_store.reason(delivery.last_error),
        })
    return {"counts": counts, "bad_count": sum(bad for _, _, bad in totals),
            "items": items, "more": len(rows) > 50}


@router.post("/deliveries/review")
async def review_deliveries(body: DeliveryIdsIn, user: AdminUser, session: SessionDep) -> dict:
    """Record review of terminal deliveries without changing their outcomes or scheduling retries."""
    now = datetime.now(timezone.utc)
    reviewed_by, _ = audit.actor(user.username)
    ids = sorted(set(body.ids))
    d = NotificationDelivery
    reviewed = sorted(await session.scalars(update(d).where(
        d.id.in_(ids), d.status == "attention", d.reviewed_at.is_(None),
        d.lease_owner == "", d.lease_until.is_(None),
    ).values(reviewed_at=now, reviewed_by=reviewed_by).returning(d.id)))
    if reviewed:
        await audit.record(session, user.username, "alert_delivery_review", "",
                           ",".join(map(str, reviewed)), required=True)
    return {"reviewed_ids": reviewed, "skipped_ids": [item for item in ids if item not in reviewed]}


@router.post("/deliveries/retry")
async def retry_deliveries(body: DeliveryRetryIn, user: AdminUser, session: SessionDep,
                           request: Request) -> dict:
    """Only enqueue an explicit retry; the normal worker owns every HTTP POST."""
    now = datetime.now(timezone.utc)
    ids = sorted(set(body.ids))
    # Persisted per-account limit survives restarts and is serialized on PostgreSQL.
    await session.execute(update(User).where(User.id == user.id).values(token_version=User.token_version))
    recent = await session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.username == user.username, AuditLog.action == "alert_delivery_retry",
        AuditLog.ts >= now - timedelta(minutes=1)))
    if recent >= 5:
        raise HTTPException(429, "Слишком много повторов доставки, повторите через минуту",
                            headers={"Retry-After": "60"})
    rows = list(await session.scalars(select(NotificationDelivery).where(NotificationDelivery.id.in_(ids))))
    uncertain = [row.id for row in rows if row.reviewed_at is None and row.status in _DELIVERY_PROBLEMS and (
        row.uncertain_count > 0 or row.status == "uncertain")]
    if uncertain and not body.acknowledge_uncertain:
        raise HTTPException(409, _RETRY_WARNING)
    # Record the explicit request before releasing the account's rate-limit lock.
    await audit.record(session, user.username, "alert_delivery_retry", "", ",".join(map(str, ids)))
    queued = await notification_store.retry(request.app.state.session_factory, ids, now,
                                            acknowledge_uncertain=body.acknowledge_uncertain)
    return {"queued_ids": queued, "skipped_ids": [item for item in ids if item not in queued],
            "delivery_confirmed": False}


async def _current(session, settings) -> AlertConfigOut:
    cfg = await settings_store.get_alert_config(session, settings)
    muted = await settings_store.get_muted(session)
    return AlertConfigOut(
        telegram_token=cfg["telegram_token"],
        telegram_chat=cfg["telegram_chat"],
        telegram_api=cfg["telegram_api"],
        webhook=cfg["webhook"],
        flood_threshold=cfg["flood_threshold"],
        enabled=alerts.alerts_enabled(cfg),
        muted=muted,
        tg_buttons=cfg["tg_buttons"],
        tz=cfg["tz"],
        tg_poll=tg_bot.poll_state_out(await settings_store.get_tg_poll(session)),
        escalation=await settings_store.get_escalation(session),
        esc_candidates=[
            {"username": u.username, "linked": bool(u.tg_chat_id)}
            for u in (await session.scalars(select(User).order_by(User.username))).all()
        ],
        routes=cfg["routes"],
        route_groups={
            "servers": sorted({g for g in await session.scalars(select(Server.group_name)) if g}),
            "sites": sorted({g for g in await session.scalars(select(Check.group_name)) if g}),
        },
        routes_state=await settings_store.get_routes_state(session),
        dup_pause={**cfg["dup_pause"], "on": alerts.dup_paused(cfg)},
    )


@router.get("", response_model=AlertConfigOut)
async def get_alerts(_: AdminUser, session: SessionDep) -> AlertConfigOut:
    return await _current(session, get_settings())


# Креды каналов уходят в пульс, а его читает root-скрипт сторожа (ops/panel-watchdog.sh): перевод
# строки дописал бы туда свои строки, а адрес вида "-K файл" curl принял бы за ключ. Токен и чат
# сторож берет по тем же правилам: что он молча не пошлет, здесь не сохраняется


def _http_url(value: str) -> bool:
    """Полный адрес http(s) с хостом и без пробелов."""
    if not value.startswith(("http://", "https://")) or any(c.isspace() for c in value):
        return False
    try:
        return bool(urlsplit(value).hostname)
    except ValueError:
        return False


def _channel_error(body: AlertConfigIn) -> str:
    """Почему значения каналов нельзя сохранить; пусто - можно. Проверяем то, что сохранится."""
    values = [v.strip() for v in (body.telegram_token, body.telegram_chat, body.telegram_api, body.webhook)]
    if any(heartbeat.CONTROL_CHARS.search(v) for v in values):
        return "Токен, Chat ID и адреса каналов не должны содержать переводы строк и управляющие символы"
    token, chat = values[0], values[1]
    if (any(c.isspace() for c in token + chat) or token.startswith("-")
            or (chat.startswith("-") and not re.fullmatch(r"-[0-9]+", chat))):
        return "Токен бота и Chat ID пишутся без пробелов, с минуса начинается только числовой Chat ID"
    if values[3] and not _http_url(values[3]):
        return "URL вебхука должен быть полным адресом http:// или https:// без пробелов"
    if values[2] and not _http_url(values[2]):
        return "Адрес API должен быть полным адресом http:// или https:// без пробелов"
    return ""


@router.put("", response_model=AlertConfigOut)
async def put_alerts(
    body: AlertConfigIn, user: AdminUser, session: SessionDep
) -> AlertConfigOut:
    settings = get_settings()
    if err := _channel_error(body):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, err)
    if body.tz.strip():
        try:
            ZoneInfo(body.tz.strip())
        except Exception:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"Неизвестный часовой пояс: {body.tz}. Пример: Europe/Moscow")
    await settings_store.set_alert_config(
        session,
        body.telegram_token,
        body.telegram_chat,
        body.webhook,
        body.telegram_api,
        body.flood_threshold,
        body.tg_buttons,
        body.tz,
    )
    if body.escalation is not None:
        await settings_store.set_escalation(session, body.escalation.model_dump())
    if body.routes is not None:
        await settings_store.set_routes(session, body.routes.model_dump())
    await settings_store.set_muted(session, body.muted)
    await audit.record(session, user.username, "alerts_config", "")
    return await _current(session, settings)


@router.post("/dup-pause", response_model=AlertConfigOut)
async def dup_pause(body: DupPauseIn, user: AdminUser, session: SessionDep) -> AlertConfigOut:
    """Пауза копий в основной чат: сразу, без "Сохранить" - это кнопка на время, а не настройка."""
    settings = get_settings()
    cfg = await settings_store.get_alert_config(session, settings)
    await settings_store.set_dup_pause(
        session, alerts.dup_pause_value(body.op, datetime.now(timezone.utc), cfg.get("tz") or "UTC", user.username))
    await audit.record(session, user.username, "alerts_dup_pause", "", body.op)
    return await _current(session, settings)


@router.post("/routes/test", response_model=AlertTestResult)
async def test_route(body: RouteTestIn, user: AdminUser, session: SessionDep) -> AlertTestResult:
    """Проверка чата по группам: дойдет ли туда сообщение общего бота."""
    cfg = await settings_store.get_alert_config(session, get_settings())
    if not cfg.get("telegram_token"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сначала укажите токен бота")
    chat = body.chat.strip()
    route = next((r for r in cfg["routes"]["chats"] if r["chat"] == chat), None)
    groups = ", ".join((route or {}).get("server_groups", []) + (route or {}).get("site_groups", []))
    text = "🔔 Проверка: сюда будут приходить алерты" + (f" групп {groups}" if groups else "") + "."
    try:
        await alerts.tg_send(cfg.get("telegram_api", ""), cfg["telegram_token"], chat, text)
    except Exception as exc:  # noqa: BLE001
        return AlertTestResult(sent=False, errors=[f"Telegram: {exc}"])
    await audit.record(session, user.username, "alerts_route_test", chat)
    return AlertTestResult(sent=True)


@router.get("/server-rules", response_model=ServerAlertRulesOut)
async def get_server_rules(_: AdminUser, session: SessionDep) -> ServerAlertRulesOut:
    rules = await settings_store.get_server_alert_rules(session)
    kinds = [
        ServerAlertKindInfo(key=k, label=label, default_text=default)
        for k, (label, default) in settings_store.SERVER_ALERT_KINDS.items()
    ]
    return ServerAlertRulesOut(rules=rules, kinds=kinds)


@router.put("/server-rules", response_model=ServerAlertRulesOut)
async def put_server_rules(
    body: ServerAlertRulesIn, user: AdminUser, session: SessionDep
) -> ServerAlertRulesOut:
    await settings_store.set_server_alert_rules(
        session, {k: v.model_dump() for k, v in body.rules.items()}
    )
    await audit.record(session, user.username, "server_alert_rules", "")
    rules = await settings_store.get_server_alert_rules(session)
    kinds = [
        ServerAlertKindInfo(key=k, label=label, default_text=default)
        for k, (label, default) in settings_store.SERVER_ALERT_KINDS.items()
    ]
    return ServerAlertRulesOut(rules=rules, kinds=kinds)


@router.get("/site-rules", response_model=ServerAlertRulesOut)
async def get_site_rules(_: AdminUser, session: SessionDep) -> ServerAlertRulesOut:
    rules = await settings_store.get_site_alert_rules(session)
    kinds = [
        ServerAlertKindInfo(key=k, label=label, default_text=default)
        for k, (label, default) in settings_store.SITE_ALERT_KINDS.items()
    ]
    return ServerAlertRulesOut(rules=rules, kinds=kinds)


@router.put("/site-rules", response_model=ServerAlertRulesOut)
async def put_site_rules(
    body: ServerAlertRulesIn, user: AdminUser, session: SessionDep
) -> ServerAlertRulesOut:
    await settings_store.set_site_alert_rules(
        session, {k: v.model_dump() for k, v in body.rules.items()}
    )
    await audit.record(session, user.username, "site_alert_rules", "")
    rules = await settings_store.get_site_alert_rules(session)
    kinds = [
        ServerAlertKindInfo(key=k, label=label, default_text=default)
        for k, (label, default) in settings_store.SITE_ALERT_KINDS.items()
    ]
    return ServerAlertRulesOut(rules=rules, kinds=kinds)


def _fixable(rules: list[dict], group: str) -> bool:
    """Можно ли одной кнопкой дописать объект в область хотя бы одного правила.

    По группам — только если группа у объекта есть: пустую в scope не положишь.
    По перечислению объектов — всегда: там лежат id.
    """
    return any(
        (r.get("scope_type") == "groups" and bool(group))
        or r.get("scope_type") in ("servers", "checks")
        for r in rules
    )


async def _coverage(session) -> list[MuteWarning]:
    """Объекты, по которым не сработает ни один алерт.

    Область действия задаётся у каждого типа отдельно, и достаточно один раз
    ограничить правила группой, чтобы всё, что заведут вне её, молчало. Молчание
    мониторинга не видно вообще: график рисуется, метрики идут, а когда нода
    ляжет — не придёт ничего. Поэтому панель обязана сказать об этом сама.

    Заглушённое ВРУЧНУЮ (alert_mutes, снуз) сюда не попадает: это осознанное
    решение, и напоминать о нём — шум.
    """
    srv_rules = await settings_store.get_server_alert_rules(session)
    site_rules = await settings_store.get_site_alert_rules(session)
    live_srv = [r for r in srv_rules.values() if r.get("enabled")]
    live_site = [r for r in site_rules.values() if r.get("enabled")]

    out: list[MuteWarning] = []
    for s in await session.scalars(select(Server).where(Server.enabled.is_(True))):
        if any(collector._rule_scope_ok(r, s) for r in live_srv):
            continue
        grp = s.group_name or ""
        out.append(MuteWarning(
            kind="server", id=s.id, name=s.name, group=grp,
            reason_code=("no_rules" if not live_srv else
                         "group_out" if grp else "no_group"),
            fixable=_fixable(live_srv, grp),
        ))
    for c in await session.scalars(select(Check).where(Check.enabled.is_(True))):
        grp = c.group_name or ""
        if any(collector._site_scope_ok(r, c.id, grp) for r in live_site):
            continue
        out.append(MuteWarning(
            kind="site", id=c.id, name=c.name, group=grp,
            reason_code=("no_rules" if not live_site else
                         "group_out" if grp else "no_group"),
            fixable=_fixable(live_site, grp),
        ))
    out.sort(key=lambda x: (x.kind, x.name))
    return out


@router.get("/coverage", response_model=AlertCoverageOut)
async def alert_coverage(_: AdminUser, session: SessionDep) -> AlertCoverageOut:
    return AlertCoverageOut(items=await _coverage(session))


def _widen(rule: dict, obj_id: int, group: str) -> bool:
    """Минимально расширить область правила так, чтобы объект в неё попал.

    Ничего не переключаем: правило «по группам» так и остаётся по группам, просто
    в списке появляется ещё одна. Тип области — решение хозяина панели, и менять
    его за него кнопка не имеет права.
    """
    st = rule.get("scope_type", "all")
    scope = list(rule.get("scope") or [])
    if st == "groups":
        if not group or group in scope:
            return False
        scope.append(group)
    elif st in ("servers", "checks"):
        if obj_id in scope:
            return False
        scope.append(obj_id)
    else:
        return False
    rule["scope"] = scope
    return True


@router.post("/coverage/apply", response_model=CoverageFixOut)
async def alert_coverage_apply(
    body: CoverageFixIn, user: AdminUser, session: SessionDep
) -> CoverageFixOut:
    """Дописать немые объекты в область уже включённых правил.

    Иначе чинить это приходится вручную и в другом месте: сообразить, что дело в
    области действия, открыть настройки, пройти по всем типам алертов и в каждом
    добавить группу. Тридцать правил — тридцать одинаковых правок.

    Выключенные правила не трогаем: расширять область того, что не работает,
    бессмысленно, а молча включать его — не наше дело.
    """
    srv_ids = {x.id for x in body.items if x.kind == "server"}
    site_ids = {x.id for x in body.items if x.kind == "site"}
    # группы берём из БД, а не из запроса: у клиента они могли устареть
    srv = {
        s.id: (s.group_name or "")
        for s in await session.scalars(select(Server).where(Server.id.in_(srv_ids)))
    } if srv_ids else {}
    site = {
        c.id: (c.group_name or "")
        for c in await session.scalars(select(Check).where(Check.id.in_(site_ids)))
    } if site_ids else {}

    n = 0
    for getter, setter, objs, what in (
        (settings_store.get_server_alert_rules,
         settings_store.set_server_alert_rules, srv, "server_alert_rules"),
        (settings_store.get_site_alert_rules,
         settings_store.set_site_alert_rules, site, "site_alert_rules"),
    ):
        if not objs:
            continue
        rules = await getter(session)
        touched = 0
        for rule in rules.values():
            if not rule.get("enabled"):
                continue
            for obj_id, group in objs.items():
                if _widen(rule, obj_id, group):
                    touched += 1
        if touched:
            await setter(session, rules)
            await audit.record(session, user.username, what, "coverage")
            n += touched

    return CoverageFixOut(rules=n, items=await _coverage(session))


@router.post("/test", response_model=AlertTestResult)
async def test_alerts(_: AdminUser, session: SessionDep) -> AlertTestResult:
    settings = get_settings()
    cfg = await settings_store.get_alert_config(session, settings)
    if not alerts.alerts_enabled(cfg):
        return AlertTestResult(sent=False, errors=["Каналы не настроены"])
    errors = await alerts.send_alert(cfg, "🔔 Kervax\nТест: алерты сюда доходят")
    return AlertTestResult(sent=not errors, errors=errors)
