"""Durable ownership of alert transitions, separate from confirmed delivery.

Telegram has no sendMessage idempotency key. A persisted attempt with a lost
response remains uncertain; a retry may produce a copy. Attempts are delayed,
bounded and visible, rather than re-created on every observation of an incident.
Credentials are resolved at send time and never copied into the journal.
"""
from __future__ import annotations

import hashlib
import html
import json
import inspect
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app import notification_store as store
from app.models import AlertEvent, AppSetting, CheckIncident, NotificationDelivery, NotificationEvent, NotificationStream, NotificationTransport, Server, TgAlert, User

log = logging.getLogger("kervax.alert_delivery")
_monotonic = time.monotonic
_FIELDS = ("section", "group", "kind", "target", "recovery", "ref", "full", "line", "mutes")


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def pack(message) -> dict:
    payload = {"text": str(message)}
    payload.update({key: getattr(message, key) for key in _FIELDS if hasattr(message, key)})
    for key in ("notice", "listen_episodes", "resolves"):
        if hasattr(message, key):
            payload[key] = getattr(message, key)
    return payload


def unpack(payload):
    from app import alerts
    message = alerts.Msg(payload["text"], **{key: payload[key] for key in _FIELDS if key in payload})
    for key in ("notice", "listen_episodes", "resolves"):
        if key in payload:
            setattr(message, key, payload[key])
    return message


def atoms(message):
    parts = getattr(message, "parts", None)
    if not parts:
        return [message]
    return [atom for part in parts for atom in atoms(part)]


def metadata(message, now: datetime) -> dict:
    """Producer identities precede notes, digests, storm merging and HTML edits."""
    notice = dict(getattr(message, "notice", None) or {})
    if not notice.get("stream"):
        identity = [getattr(message, key, "") for key in ("section", "kind", "ref", "target")]
        # Unstructured one-shot callers do not acquire an accidental permanent
        # incident merely because two people requested the same test message.
        if not any(identity):
            notice["occurrence"] = uuid.uuid4().hex
        notice["stream"] = "legacy:" + _hash(identity)
    notice.setdefault("phase", "recovery" if getattr(message, "recovery", False) else "fire")
    notice.setdefault("members", sorted(set(getattr(message, "mutes", None) or [])))
    notice.setdefault("level", 2 if str(message).startswith(("🔴", "🔥")) else 1)
    notice["observed_at"] = _utc(notice.get("observed_at") or now)
    return notice


def _telegram_identity(api, token, chat):
    transport = _hash("telegram", (api or "https://api.telegram.org").rstrip("/"), token)
    return _hash(transport, str(chat)), transport


async def recipients(factory, cfg: dict, message) -> list[dict]:
    from app import alerts
    token, api = cfg.get("telegram_token") or "", cfg.get("telegram_api") or ""
    found = {}

    def add(chat, current_token, target):
        if not chat or not current_token:
            return
        key, transport = _telegram_identity(api, current_token, chat)
        target = {**target, "type": "telegram", "chat": str(chat)}
        # A personal subscription and a common route can name the same actual
        # chat. One HTTP request satisfies both without weakening route scope.
        if key not in found or target.get("required"):
            found[key] = {"key": key, "transport": transport, "target": target}

    main = str(cfg.get("telegram_chat") or "")
    for chat in alerts.chats_for(cfg, message) if token else []:
        add(chat, token, {"bot": "common", "required": True,
                          "route": "main" if str(chat) == main else "group"})
    if cfg.get("webhook"):
        key = _hash("webhook", cfg["webhook"])
        found[key] = {"key": key, "transport": key,
                      "target": {"type": "webhook", "required": True}}
    async with factory() as session:
        users = list(await session.scalars(select(User).where(User.tg_alerts.is_(True), User.tg_chat_id != "")))
    for user in users:
        if alerts.wants(user, message):
            own = bool(user.tg_token) and user.tg_token != token
            add(user.tg_chat_id, user.tg_token or token,
                {"bot": "personal" if own else "common", "user_id": user.id,
                 "route": "personal", "required": False})
    return list(found.values())


async def admit(factory, cfg, messages, threshold, parse_mode, now, on_accept=None):
    from app import alerts, settings_store
    specs, origins = [], []
    for index, message in enumerate(messages):
        parts = atoms(message)
        envelope = pack(message) if getattr(message, "parts", None) else None
        streams = sorted(metadata(atom, now)["stream"] for atom in parts)
        for part_index, atom in enumerate(parts):
            notice = metadata(atom, now)
            destinations = await recipients(factory, cfg, atom)
            if not destinations:
                continue
            packed = pack(atom)
            packed["notice"] = {**notice, "observed_at": notice["observed_at"].isoformat()}
            payload = {"message": packed, "parse_mode": parse_mode, "threshold": threshold,
                       "batch_order": index * 10000 + part_index}
            if envelope:
                payload.update(envelope=envelope, envelope_streams=streams,
                               envelope_key=_hash(envelope, streams))
            specs.append({**notice, "payload": payload, "recipients": destinations})
            origins.append(index)
    called = False
    callbacks = set()
    expected = {index: origins.count(index) for index in set(origins)}

    async def callback(session, index, event_id):
        nonlocal called
        callbacks.add(index)
        spec = specs[index]
        message = unpack(spec["payload"]["message"])
        if (spec["phase"] != "recovery" and alerts.dup_paused(cfg) and alerts.critical(message)
                and alerts.route_hits(cfg, message) and any(r["target"].get("route") == "main"
                                                          for r in spec["recipients"])):
            current = await settings_store.get_main_passed(session)
            wanted = current + [list(pair) for pair in alerts._pairs(message) if list(pair) not in current]
            if wanted != current:
                row = await session.get(AppSetting, settings_store.MAIN_PASSED_KEY)
                if row is None:
                    session.add(AppSetting(key=settings_store.MAIN_PASSED_KEY, value=json.dumps(wanted[-300:])))
                else:
                    row.value = json.dumps(wanted[-300:])
        complete = {original for original in expected if sum(
            origins[item] == original for item in callbacks) == expected[original]}
        if on_accept is not None and len(inspect.signature(on_accept).parameters) > 1:
            await on_accept(session, complete)
        elif on_accept is not None and not called and len(callbacks) == len(specs):
            # Admission is one transaction for the complete batch. The observer
            # patch and every recipient job either commit together or roll back.
            await on_accept(session)
            called = True

    ids = await store.admit(factory, specs, now, callback=callback)
    accepted, event_map = set(), defaultdict(list)
    for index, event_id in zip(origins, ids, strict=True):
        event_map[index].append(event_id)
    for index, events in event_map.items():
        if events and all(event is not None for event in events):
            accepted.add(index)
    return accepted, dict(event_map)


async def _configuration(factory, cfg, row, message):
    from app import alerts
    target = row["target"]
    if target["type"] == "webhook":
        if not cfg.get("webhook") or _hash("webhook", cfg["webhook"]) != row["recipient_key"]:
            return None
        return {**cfg, "telegram_token": "", "telegram_chat": ""}
    token = cfg.get("telegram_token") or ""
    if target.get("route") == "fallback" and target["chat"] != str(cfg.get("telegram_chat") or ""):
        return None
    if target.get("user_id") is not None:
        async with factory() as session:
            user = await session.get(User, target["user_id"])
        if user is None or not user.tg_alerts or not alerts.wants(user, message):
            return None
        if str(user.tg_chat_id) != target["chat"]:
            return None
        token = user.tg_token or token
    elif target.get("route") in ("main", "group"):
        owned_main = target.get("recovery_owned") and target["route"] == "main" and (
            str(cfg.get("telegram_chat") or "") == target["chat"])
        if not owned_main and target["chat"] not in {str(chat) for chat in alerts.chats_for(cfg, message)}:
            return None
    key, transport = _telegram_identity(cfg.get("telegram_api"), token, target["chat"])
    if not token or key != row["recipient_key"] or transport != row["transport_key"]:
        return None
    return {**cfg, "telegram_token": token, "telegram_chat": target["chat"], "webhook": ""}


async def _wanted(factory, row, message, now):
    from app import listen_watch
    async with factory() as session:
        if action := row["payload"].get("action"):
            from app import alert_delivery_actions
            if not await alert_delivery_actions.wanted(session, action, now):
                return False
        episodes = getattr(message, "listen_episodes", None)
        if episodes and not message.recovery:
            sid = (getattr(message, "notice", {}) or {}).get("source_id")
            if sid is None:
                from app import alerts
                sid = alerts._listen_server(message)
            if not listen_watch.can_notify(await session.get(Server, sid), episodes, now):
                return False
    return True


async def _button(factory, cfg, event_id, message) -> int | None:
    from app import alerts, tg_buttons
    if not alerts.buttons_on(cfg) or not tg_buttons.wants_buttons(message):
        return None
    event = (await store.load_events(factory, [event_id]))[event_id]
    if event.get("tg_alert_id"):
        return event["tg_alert_id"]
    notice = getattr(message, "notice", {}) or {}
    if notice.get("edit_group"):
        from app import site_group
        async with factory() as session:
            state = await site_group.load(session)
            receipts = (state.get(notice["edit_group"]) or {}).get("msgs") or notice.get("group_messages") or []
            where = {(str(r["chat"]), r["id"]) for r in receipts}
            candidates = await session.scalars(select(TgAlert).where(TgAlert.kind == message.kind)
                                               .order_by(TgAlert.id.desc()).limit(200))
            for row in candidates:
                if any((str(r["chat"]), r["id"]) in where for r in row.messages or []):
                    return await store.set_tg_alert(factory, event_id, row.id)
    created = await tg_buttons.prepare(factory, [message])
    return await store.set_tg_alert(factory, event_id, created[0]) if 0 in created else None


async def _coalesce_closed(factory, now):
    """Replace obsolete unconfirmed fire copies with a completed-incident notice.

    The original transitions and ambiguity counters remain in the journal. Only
    an owned recovery for the same episode AND recipient can cover their jobs.
    An active HTTP lease is never cancelled underneath another sender.
    """
    await store.due(factory, now, limit=0)  # expire abandoned POSTs before consolidation
    async with factory() as session:
        keys = sorted(set(await session.scalars(select(NotificationDelivery.transport_key).join(
            NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id).where(
            NotificationDelivery.status.in_(("pending", "uncertain", "attention")),
            NotificationEvent.phase == "recovery"))))
        for key in keys:
            await session.execute(update(NotificationTransport).where(NotificationTransport.key == key)
                                  .values(failures=NotificationTransport.failures))
        pairs = list((await session.execute(select(NotificationDelivery, NotificationEvent).join(
            NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id).where(
            NotificationDelivery.status.in_(("pending", "uncertain", "attention")),
            NotificationEvent.phase == "recovery").order_by(NotificationDelivery.id)
            .with_for_update(of=NotificationDelivery))).all())
        for recovery, event in pairs:
            older = list((await session.execute(select(NotificationDelivery, NotificationEvent).join(
                NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id).where(
                NotificationEvent.stream_key == event.stream_key,
                NotificationEvent.episode == event.episode,
                NotificationEvent.seq < event.seq,
                NotificationDelivery.recipient_key == recovery.recipient_key,
                NotificationDelivery.status.in_(("pending", "uncertain", "attention", "sending")))
                .order_by(NotificationDelivery.id).with_for_update(of=NotificationDelivery))).all())
            if any(job.status == "sending" and job.lease_until and _utc(job.lease_until) > now for job, _ in older):
                continue
            covered = list((recovery.target or {}).get("covered_events") or [])
            for job, prior in older:
                if prior.id not in covered:
                    covered.append(prior.id)
                job.status = "superseded"
                job.target = {**(job.target or {}), "superseded_by": event.id}
                job.lease_owner, job.lease_until = "", None
            if covered:
                recovery.target = {**(recovery.target or {}), "covered_events": covered}
        await session.commit()


async def _render(factory, row, now):
    from app import alerts
    message = unpack(row["payload"]["message"])
    message.recovery = row["phase"] == "recovery"
    covered = row["target"].get("covered_events") or []
    if covered:
        events = await store.load_events(factory, covered)
        first = next((value for value in sorted(events.values(), key=lambda e: e["seq"])
                      if value["phase"] == "fire"), None)
        if first:
            original = unpack(first["payload"]["message"])
            short = alerts._digest_line(original)
            prior_message = message
            message = alerts.Msg(f"✅ {html.escape(message.target)}: восстановилось\n"
                                 f"Пока уведомления не доходили: {short}",
                                 message.section, message.group, message.kind, message.target,
                                 True, message.ref,
                                 full=alerts.full_text(original) + "\n" + alerts.full_text(message),
                                 mutes=message.mutes)
            for key in ("notice", "resolves", "listen_episodes"):
                if hasattr(prior_message, key):
                    setattr(message, key, getattr(prior_message, key))
    return _delivery_note(message, row, now)


def _delivery_note(message, row, now):
    from app import alerts
    if row["target"].get("covered_events"):
        pass  # The completed-incident summary already explains the delay.
    elif row.get("uncertain_count"):
        # This is explicitly a delayed verification copy, never a new incident.
        prior = message
        label = "Повтор: предыдущая доставка не подтверждена"
        severity = next((icon for icon in ("🚨", "🔴", "🔥", "⚠️", "✅")
                         if str(prior).startswith(icon)), "")
        message = alerts.Msg((severity + " " if severity else "") + label + "\n" + str(prior))
        message.__dict__.update(prior.__dict__)
        message.full = alerts.full_text(prior) + "\nПредыдущая попытка доставки не подтверждена."
        # Put the warning first, including in digests and four-line copies.
        # A tail can disappear when the notification budget is already full.
        message.line = (severity + " " if severity else "") + label + " · " + alerts._digest_line(prior)
    elif now - _utc(row["occurred_at"]) > timedelta(minutes=10):
        at = _utc(row["occurred_at"]).strftime("%H:%M UTC")
        message = message.tail("\nОтправлено после восстановления доставки.",
                               f"\nСобытие в {at}, доставка задержалась")
    if row["target"].get("reason") == "group_rejected":
        message = message.tail("\nЧат группы отклонил уведомление; копия доставлена в основной чат.",
                               "\nКопия: чат группы отклонил уведомление")
    return alerts.short_message(message, row["payload"].get("parse_mode"))


async def _history(factory, event_ids, covered=None, rendered=None):
    from app import alerts, listen_watch
    events = await store.load_events(factory, event_ids)

    async def write(session, ids):
        for event_id in ids:
            event = events[event_id]
            if event["payload"].get("skip_history"):
                continue
            message = (rendered or {}).get(event_id) or unpack(event["payload"]["message"])
            text = alerts.full_text(message)
            if event_id in (covered or set()):
                text += "\nСобытие сообщено вместе с восстановлением после задержки доставки."
            session.add(AlertEvent(kind=message.kind[:40], target=message.target[:255],
                                   section=message.section[:20], grp=message.group[:255],
                                   recovery=event["phase"] == "recovery",
                                   text=html.unescape(alerts._TAG.sub("", text))[:4000]))
            notice = getattr(message, "notice", None) or {}
            if notice.get("incident_id") and event["phase"] != "recovery":
                incident = await session.get(CheckIncident, notice["incident_id"])
                if incident is not None:
                    incident.notified = True
            if sid := alerts._listen_server(message):
                srv = await session.get(Server, sid, with_for_update=True)
                if srv is not None:
                    active = listen_watch.matching(srv.alert_state or {}, message.listen_episodes)
                    if active:
                        srv.alert_state = listen_watch.mark_delivered(srv.alert_state or {}, active,
                                                                    datetime.now(timezone.utc))
                    elif not (srv.alert_state or {}).get("listen_gone"):
                        ports = ", ".join(key.removeprefix("tcp:") for key in message.listen_episodes)
                        session.add(AlertEvent(kind=message.kind[:40], target=message.target[:255],
                            section=message.section[:20], grp=message.group[:255], recovery=True,
                            text=listen_watch.closure(message.target, ports)))
    await store.mark_history(factory, event_ids, callback=write)


async def _resolve_buttons(factory, cfg, allowed, now):
    """An old delayed recovery must never resolve buttons of a newer episode."""
    from app import alerts, tg_buttons
    async with factory() as session:
        aids = set()
        for delivery, message, _ in allowed:
            if delivery["phase"] != "recovery":
                continue
            aids.update(value for value in await session.scalars(select(NotificationEvent.tg_alert_id).where(
                NotificationEvent.stream_key == delivery["stream_key"],
                NotificationEvent.episode == delivery["episode"],
                NotificationEvent.seq < delivery["seq"], NotificationEvent.tg_alert_id.is_not(None))) if value)
            # Buttons created before the outbox migration have no episode link.
            # The observation timestamp fences them from later incidents.
            legacy = await session.scalars(select(TgAlert).where(
                TgAlert.kind == message.kind, TgAlert.resolved_at.is_(None),
                TgAlert.ts <= delivery["occurred_at"],
                ~select(NotificationEvent.id).where(NotificationEvent.tg_alert_id == TgAlert.id).exists()))
            for row in legacy:
                if row.target == message.target or await alerts._storm_back(
                        session, row, {(message.kind, message.target)}):
                    aids.add(row.id)
        resolved = []
        for aid in sorted(aids):
            associated = list(await session.scalars(select(NotificationEvent).where(
                NotificationEvent.tg_alert_id == aid, NotificationEvent.phase != "recovery")))
            # Group buttons remain active while any original member episode is open.
            open_member = False
            for event in associated:
                stream = await session.get(NotificationStream, event.stream_key)
                if stream and stream.episode == event.episode and stream.phase != "recovery":
                    open_member = True
                    break
            if open_member:
                continue
            row = await session.get(TgAlert, aid, with_for_update=True)
            if row is not None and row.resolved_at is None:
                row.resolved_at = now
                resolved.append(row)
        await session.commit()
        for row in resolved:
            text = tg_buttons.row_text(row, cfg.get("tz") or "UTC", now)
            for receipt in row.messages or []:
                await alerts.tg_edit(cfg, receipt["chat"], receipt["id"], text, tg_buttons.row_keyboard(row))


def _messages(rows, now=None):
    """Retain a grouped display only when every constituent is authorized here."""
    displayed, used = [], set()
    for row, message, _ in rows:
        if row["id"] in used:
            continue
        envelope = row["payload"].get("envelope")
        key = row["payload"].get("envelope_key")
        peers = [item for item in rows if key and item[0]["payload"].get("envelope_key") == key]
        streams = sorted((item[1].notice or {}).get("stream", "") for item in peers)
        if envelope and streams == row["payload"].get("envelope_streams") and not any(
                item[0]["target"].get("covered_events") for item in peers):
            parent = unpack(envelope)
            parent.parts = [item[1] for item in peers]
            if now is not None:
                # Display envelopes must retain delivery warnings too. Rendering
                # only their atoms would hide an ambiguous retry's copy marker.
                delivery = dict(row)
                delivery["uncertain_count"] = max(item[0].get("uncertain_count", 0) for item in peers)
                delivery["occurred_at"] = min(item[0]["occurred_at"] for item in peers)
                parent = _delivery_note(parent, delivery, now)
            displayed.append(parent)
            used.update(item[0]["id"] for item in peers)
        else:
            displayed.append(message)
            used.add(row["id"])
    return displayed


async def _site_receipt(factory, allowed, receipts):
    from app import site_group
    common = [item for item in allowed if item[0]["target"].get("route") != "personal"
              and item[1].section == "sites"]
    if not common or not receipts:
        return
    async with factory() as session:
        await session.execute(update(AppSetting).where(AppSetting.key == site_group.STATE_KEY)
                              .values(value=AppSetting.value))
        await session.get(AppSetting, site_group.STATE_KEY, with_for_update=True)
        state = await site_group.load(session)
        changed = False
        for _, message, _ in common:
            notice = getattr(message, "notice", {}) or {}
            cid = str(notice.get("check_id") or "")
            for gid, group in state.items():
                if notice.get("site_group") and notice["site_group"] != gid:
                    continue
                if cid in (group.get("members") or {}) and group.get("kind") == message.kind:
                    msgs = list(group.get("msgs") or [])
                    for receipt in receipts:
                        if receipt not in msgs:
                            msgs.append(receipt)
                    group["msgs"] = msgs
                    changed = True
        if changed:
            await site_group.save(session, state)


async def _edit_group(factory, configuration, allowed, message, aid=None):
    """Editing a known message is idempotent, but its result still needs checking."""
    from app import alerts, site_group
    gids = {(getattr(current, "notice", {}) or {}).get("edit_group") for _, current, _ in allowed}
    if len(gids) != 1 or not next(iter(gids)):
        return None
    async with factory() as session:
        state = await site_group.load(session)
    group = state.get(next(iter(gids))) or {}
    snapshot = (getattr(message, "notice", {}) or {}).get("group_messages") or []
    receipts = [receipt for receipt in group.get("msgs") or snapshot
                if str(receipt.get("chat")) == str(configuration.get("telegram_chat"))]
    if not receipts:
        return None  # No previously confirmed copy exists in this destination.
    errors = alerts.SendErrors()
    text, markup = str(message), None
    if aid:
        from app import tg_buttons
        async with factory() as session:
            row = await session.get(TgAlert, aid)
            if row is not None:
                row.text, row.ref = str(message), message.ref[:255]
                text = tg_buttons.row_text(row, configuration.get("tz") or "UTC")
                markup = tg_buttons.row_keyboard(row)
    base = (configuration.get("telegram_api") or "https://api.telegram.org").rstrip("/")
    async with alerts._link() as link:
        for receipt in receipts:
            payload = {
                    "chat_id": receipt["chat"], "message_id": receipt["id"],
                    "text": alerts._clip(text, True), "parse_mode": "HTML",
                    "disable_web_page_preview": True}
            if markup is not None:
                payload["reply_markup"] = markup
            response = await (await link.client()).post(
                f"{base}/bot{configuration['telegram_token']}/editMessageText", json=payload)
            code = alerts._status(response)
            unchanged = code == 400 and "message is not modified" in alerts._tg_why(response).lower()
            if code >= 400 and not unchanged:
                if 400 <= code < 500 and code != 429:
                    errors.rejected += 1
                errors.append(f"HTTP {code}")
            else:
                errors.receipts.append(receipt)
    return errors


async def _save_buttons(factory, aids, receipts):
    if not receipts:
        return
    async with factory() as session, session.begin():
        for aid in sorted(set(aids) - {None}):
            row = await session.get(TgAlert, aid, with_for_update=True)
            if row is not None:
                row.messages = list(row.messages or []) + [r for r in receipts if r not in (row.messages or [])]


async def _hold_confirmed(factory, cfg, allowed, aids):
    from app import alerts
    if not alerts.dup_paused(cfg) or not any(row["target"].get("route") == "group" and alerts.critical(message)
                                           for row, message, _ in allowed):
        return
    async with factory() as session, session.begin():
        for aid in sorted(set(aids) - {None}):
            row = await session.get(TgAlert, aid, with_for_update=True)
            if (row is None or not row.messages or row.acked_by or row.resolved_at
                    or "mute" in (row.undo or {})):
                continue
            if await session.get(NotificationStream, store.key(f"tg-action:{aid}:held")) is None:
                row.undo = {**(row.undo or {}), "held": str(cfg.get("telegram_chat") or "")}


async def _post(factory, cfg, rows, now):
    from app import alerts, tg_buttons
    owner = uuid.uuid4().hex
    started = _monotonic()
    claimed = await store.claim(factory, [row["id"] for row in rows], owner, now)
    claimed.sort(key=lambda row: (row["occurred_at"], row["payload"].get("batch_order", 0), row["id"]))
    if not claimed:
        return
    allowed, rejected, obsolete = [], [], []
    for row in claimed:
        message = await _render(factory, row, now)
        if not await _wanted(factory, row, message, now):
            obsolete.append(row["id"])
            continue
        configuration = await _configuration(factory, cfg, row, message)
        (allowed if configuration is not None else rejected).append((row, message, configuration))
    if rejected:
        await store.finish(factory, [row["id"] for row, _, _ in rejected], owner, "rejected", now,
                           error="destination_changed")
    if obsolete:
        await store.finish(factory, obsolete, owner, "superseded", now)
    if not allowed:
        return
    ids = [row["id"] for row, _, _ in allowed]
    event_ids = [row["event_id"] for row, _, _ in allowed]
    texts = alerts.merge_storm(_messages(allowed, now))
    message = alerts._digest(texts) if len(texts) > 1 else texts[0]
    configuration = allowed[0][2]
    aid = await _button(factory, configuration, event_ids[0], message) if (
        len(texts) == 1 and allowed[0][0]["target"].get("bot") != "personal") else None
    if aid:
        for event_id in event_ids[1:]:
            await store.set_tg_alert(factory, event_id, aid)
    sent = []
    try:
        # Preserve the established transport seam used by integrations. A real
        # Telegram response without a message id is marked uncertain there.
        errors = await _edit_group(factory, configuration, allowed, message, aid)
        if errors is not None:
            pass
        elif aid:
            markup = tg_buttons.keyboard(aid, getattr(message, "kind", ""))
            errors = await alerts.send_alert(configuration, message,
                allowed[0][0]["payload"].get("parse_mode"), markup, sent)
        else:
            errors = await alerts.send_alert(configuration, message,
                allowed[0][0]["payload"].get("parse_mode"))
        sent.extend(receipt for receipt in getattr(errors, "receipts", []) if receipt not in sent)
        if (not errors and isinstance(errors, alerts.SendErrors)
                and allowed[0][0]["target"]["type"] == "telegram" and not sent):
            errors.uncertain = True
            errors.append("Telegram response did not confirm a message id")
    except Exception as exc:
        errors = alerts.SendErrors([type(exc).__name__])
        errors.uncertain = alerts._tg_uncertain(exc)
    status = "confirmed" if not errors else "uncertain" if getattr(errors, "uncertain", False) else \
             "rejected" if getattr(errors, "rejected", 0) == len(errors) else "retry"
    completed_at = now + timedelta(seconds=max(0, _monotonic() - started))
    finished = await store.finish(factory, ids, owner, status, completed_at,
                       message_id=sent[0]["id"] if sent else None,
                       error="ReadTimeout" if status == "uncertain" else "TransportError" if errors else "",
                       retry_after=getattr(errors, "retry_after", None),
                       target_patch={"editable": len(texts) == 1})
    actual = {item[0]["id"] for item in allowed} & set(finished)
    allowed = [item for item in allowed if item[0]["id"] in actual]
    event_ids = [row["event_id"] for row, _, _ in allowed]
    if status == "rejected" and actual:
        from app import alert_delivery_fallback
        await alert_delivery_fallback.enqueue(factory, cfg, [row for row, _, _ in allowed], completed_at)
    if status == "confirmed" and actual:
        covered = {event for row, _, _ in allowed for event in row["target"].get("covered_events") or []}
        await _history(factory, sorted(set(event_ids) | covered), covered,
                       {row["event_id"]: current for row, current, _ in allowed})
        existing = await store.load_events(factory, event_ids)
        aids = [event.get("tg_alert_id") for event in existing.values()
                if len(texts) == 1 and allowed[0][0]["target"].get("bot") != "personal"]
        await _save_buttons(factory, aids, sent)
        if aid and (getattr(message, "notice", {}) or {}).get("edit_group"):
            async with factory() as session, session.begin():
                original = await session.get(TgAlert, aid, with_for_update=True)
                if original is not None:
                    original.text, original.ref = str(message), message.ref[:255]
        if len(texts) == 1:
            await _site_receipt(factory, allowed, sent)
        await alerts._note_passed(factory, cfg, [item[1] for item in allowed],
            [item[0]["target"].get("route") == "main" for item in allowed], set(range(len(allowed))))
        await _hold_confirmed(factory, cfg, allowed, aids)
        await _resolve_buttons(factory, cfg, allowed, completed_at)
        await _effects_done(factory, actual)


async def _effects_done(factory, ids):
    async with factory() as session, session.begin():
        for row in await session.scalars(select(NotificationDelivery).where(
                NotificationDelivery.id.in_(ids), NotificationDelivery.status == "confirmed")
                .order_by(NotificationDelivery.id).with_for_update()):
            row.target = {**row.target, "effects_recorded": True}


async def _reconcile_confirmed(factory, cfg, now):
    """A restart after a confirmed POST repairs metadata without repeating POST."""
    from app import alerts
    async with factory() as session:
        event_ids = list(await session.scalars(select(NotificationDelivery.event_id).where(
            NotificationDelivery.status == "confirmed",
            NotificationDelivery.target["effects_recorded"].as_boolean().is_not(True))
            .order_by(NotificationDelivery.id).limit(100)))
    jobs = [row for row in await store.deliveries(factory, event_ids)
            if row["status"] == "confirmed" and not row["target"].get("effects_recorded")]
    for row in jobs:
        message = await _render(factory, row, now)
        current = await _configuration(factory, cfg, row, message)
        allowed = [(row, message, current)]
        await _history(factory, [row["event_id"]], rendered={row["event_id"]: message})
        if current and row["target"].get("type") == "telegram" and row["message_id"]:
            receipts = [{"chat": row["target"]["chat"], "id": row["message_id"]}]
            if row["target"].get("editable") and row["target"].get("bot") != "personal":
                await _save_buttons(factory, [row.get("tg_alert_id")], receipts)
                await _site_receipt(factory, allowed, receipts)
                await _hold_confirmed(factory, cfg, allowed, [row.get("tg_alert_id")])
                envelope = row["payload"].get("envelope") or {}
                if row.get("tg_alert_id") and (envelope.get("notice") or {}).get("edit_group"):
                    async with factory() as session, session.begin():
                        original = await session.get(TgAlert, row["tg_alert_id"], with_for_update=True)
                        if original is not None:
                            original.text = envelope["text"]
                            original.ref = envelope.get("ref", "")[:255]
            await alerts._note_passed(factory, cfg, [message],
                                      [row["target"].get("route") == "main"], {0})
            await _resolve_buttons(factory, cfg, allowed, now)
        await _effects_done(factory, [row["id"]])


async def drain(factory, cfg, now=None, *, limit=100):
    """Claim recipient jobs before POST; never send without durable ownership."""
    now = _utc(now or datetime.now(timezone.utc))
    started = _monotonic()

    def current_time():
        return now + timedelta(seconds=max(0, _monotonic() - started))
    from app import alerts, settings_store
    async with factory() as session:
        if await settings_store.get_muted(session) or not alerts.alerts_enabled(cfg):
            return
    await _reconcile_confirmed(factory, cfg, current_time())
    await _coalesce_closed(factory, current_time())
    due = await store.due(factory, current_time(), limit=limit)
    due.sort(key=lambda row: (row["occurred_at"], row["payload"].get("batch_order", 0), row["id"]))
    groups = defaultdict(list)
    for row in due:
        groups[(row["recipient_key"], row["transport_key"], row["payload"].get("parse_mode"))].append(row)
    for rows in groups.values():
        threshold = int(rows[0]["payload"].get("threshold") or 0)
        displayed = _messages([(row, unpack(row["payload"]["message"]), None) for row in rows])
        if threshold and len(alerts.merge_storm(displayed)) >= threshold:
            await _post(factory, cfg, rows, current_time())
        else:
            parent = list(range(len(rows)))

            def find(index):
                while parent[index] != index:
                    index = parent[index]
                return index

            def join(indices):
                for index in indices[1:]:
                    parent[find(index)] = find(indices[0])

            envelopes = defaultdict(list)
            for index, row in enumerate(rows):
                if key := row["payload"].get("envelope_key"):
                    envelopes[key].append(index)
            for indices in envelopes.values():
                join(indices)
            origins = []
            alerts.merge_storm([unpack(row["payload"]["message"]) for row in rows], origins)
            for indices in origins:
                join(indices)
            bundles = defaultdict(list)
            for index, row in enumerate(rows):
                bundles[find(index)].append(row)
            for bundle in bundles.values():
                await _post(factory, cfg, bundle, current_time())


async def dispatch(cfg, can_send, messages, threshold, parse_mode, factory, *,
                   accepted=None, finished=None, on_accept=None, now=None):
    now = _utc(now or datetime.now(timezone.utc))
    if not can_send:
        return False
    queued, event_map = await admit(factory, cfg, messages, threshold, parse_mode, now, on_accept)
    if accepted is not None:
        accepted.update(queued)
    await drain(factory, cfg, now)
    all_ids = [event for events in event_map.values() for event in events if event is not None]
    deliveries = await store.deliveries(factory, all_ids)
    complete = {event_id for event_id in all_ids if any(
        row["event_id"] == event_id and row["target"].get("required") and row["status"] == "confirmed"
        for row in deliveries)}
    terminal = complete | {event_id for event_id in all_ids if any(
        row["event_id"] == event_id and row["target"].get("required") for row in deliveries) and all(
        row["status"] in ("confirmed", "rejected")
        for row in deliveries if row["event_id"] == event_id and row["target"].get("required"))}
    confirmed = {index for index, events in event_map.items() if events and all(event in terminal for event in events)}
    for index, events in event_map.items():
        receipts = [{"chat": row["target"]["chat"], "id": row["message_id"]} for row in deliveries
                    if row["event_id"] in events and row["status"] == "confirmed" and row["message_id"]
                    and row["target"].get("editable")
                    and row["target"].get("route") != "personal" and row["target"].get("type") == "telegram"]
        for receipt in receipts:
            sent_to = getattr(messages[index], "sent_to", None)
            if isinstance(sent_to, list) and receipt not in sent_to:
                sent_to.append(receipt)
    if finished is not None:
        finished.update(confirmed)
    # Nothing queued while a caller requested delivery remains distinguishable
    # from an empty maintenance flush. Collector ownership uses accepted only.
    return len(confirmed) == len(messages)
