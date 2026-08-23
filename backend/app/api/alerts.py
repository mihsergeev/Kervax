from fastapi import APIRouter

from app import alerts, audit, settings_store
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.schemas import (
    AlertConfigIn,
    AlertConfigOut,
    AlertTestResult,
    ServerAlertKindInfo,
    ServerAlertRulesIn,
    ServerAlertRulesOut,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])


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
    )


@router.get("", response_model=AlertConfigOut)
async def get_alerts(_: AdminUser, session: SessionDep) -> AlertConfigOut:
    return await _current(session, get_settings())


@router.put("", response_model=AlertConfigOut)
async def put_alerts(
    body: AlertConfigIn, user: AdminUser, session: SessionDep
) -> AlertConfigOut:
    settings = get_settings()
    await settings_store.set_alert_config(
        session,
        body.telegram_token,
        body.telegram_chat,
        body.webhook,
        body.telegram_api,
        body.flood_threshold,
    )
    await settings_store.set_muted(session, body.muted)
    await audit.record(session, user.username, "alerts_config", "")
    return await _current(session, settings)


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


@router.post("/test", response_model=AlertTestResult)
async def test_alerts(_: AdminUser, session: SessionDep) -> AlertTestResult:
    settings = get_settings()
    cfg = await settings_store.get_alert_config(session, settings)
    if not alerts.alerts_enabled(cfg):
        return AlertTestResult(sent=False, errors=["Каналы не настроены"])
    errors = await alerts.send_alert(cfg, "🔔 Тестовое уведомление — каналы работают.")
    return AlertTestResult(sent=not errors, errors=errors)
