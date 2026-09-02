from fastapi import APIRouter

from sqlalchemy import select

from app import alerts, audit, collector, settings_store
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.models import Check, Server
from app.schemas import (
    MuteWarning,
    AlertCoverageOut,
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


@router.get("/coverage", response_model=AlertCoverageOut)
async def alert_coverage(_: AdminUser, session: SessionDep) -> AlertCoverageOut:
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
        out.append(MuteWarning(
            kind="server", id=s.id, name=s.name, group=s.group_name or "",
            reason=("сервер вне группы, а все правила ограничены группами"
                    if not s.group_name else
                    f"группа «{s.group_name}» не входит ни в одну область алертов"),
        ))
    for c in await session.scalars(
        select(Check).where(Check.enabled.is_(True))
    ):
        grp = c.group_name or ""
        if any(collector._site_scope_ok(r, c.id, grp) for r in live_site):
            continue
        out.append(MuteWarning(
            kind="site", id=c.id, name=c.name, group=grp,
            reason=("монитор вне группы, а все правила ограничены группами"
                    if not grp else
                    f"группа «{grp}» не входит ни в одну область алертов"),
        ))
    out.sort(key=lambda x: (x.kind, x.name))
    return AlertCoverageOut(items=out)


@router.post("/test", response_model=AlertTestResult)
async def test_alerts(_: AdminUser, session: SessionDep) -> AlertTestResult:
    settings = get_settings()
    cfg = await settings_store.get_alert_config(session, settings)
    if not alerts.alerts_enabled(cfg):
        return AlertTestResult(sent=False, errors=["Каналы не настроены"])
    errors = await alerts.send_alert(cfg, "🔔 Тестовое уведомление — каналы работают.")
    return AlertTestResult(sent=not errors, errors=errors)
