"""Durable notification ownership, attempts and circuits; never performs network I/O.

An admitted event is an observed transition, not a delivered message. Credentials
are resolved by the worker at send time and must not be included in target data.
Every mutating operation commits independently of the caller's HTTP operation.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
import weakref
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from app.models import NotificationDelivery as Delivery
from app.models import NotificationEvent as Event
from app.models import NotificationStream as Stream
from app.models import NotificationTransport as Transport

UNCERTAIN_DELAY = 15 * 60
_monotonic = time.monotonic
MAX_ATTEMPTS = 6
MAX_UNCERTAIN = 3
# подготовка POST упала (alert_delivery): отсрочка строки и сколько раз подряд до attention
PREPARE_DELAY = 5 * 60
MAX_PREPARE = 3
# пачка упала до POST, виновная строка неизвестна: столько секунд строки пачки идут по одной
SOLO_WINDOW = 60 * 60
_PREPARE_MARKS = frozenset({"prepare_failures", "solo_until"})
_READY = ("pending", "uncertain")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def key(value: str) -> str:
    """Hash identity inputs before persisting them (no raw tokens or route URLs)."""
    return hashlib.sha256(str(value).encode()).hexdigest()


def _json(value):
    # Make detached copies, reject non-JSON values and non-finite counters.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _target(value: dict) -> dict:
    value = _json(value)
    if not isinstance(value, dict):
        raise ValueError("recipient target must be an object")

    def check(item):
        if isinstance(item, dict):
            for name, child in item.items():
                if re.search(r"token|password|secret|authorization|credential", name, re.I):
                    raise ValueError("credentials must be resolved outside the notification store")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, str) and "://" in item:
            parsed = urlsplit(item)
            if parsed.username or parsed.password or parsed.query or re.search(r"/bot[^/]+", parsed.path, re.I):
                raise ValueError("credential-bearing URLs must not be persisted")

    check(value)
    return value


def _identity(value: str) -> str:
    # Public callers may supply an already hashed key. Do not hash it again.
    value = str(value)
    return value if re.fullmatch(r"[a-f0-9]{64}", value) else key(value)


async def _insert_once(session, model, values: dict, columns: list[str]) -> None:
    dialect = session.bind.dialect.name
    insert = sqlite_insert if dialect == "sqlite" else pg_insert
    await session.execute(insert(model).values(**values).on_conflict_do_nothing(index_elements=columns))


async def _callback(callback, *args) -> None:
    if callback is not None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result


async def admit(factory, specs: list[dict], now: datetime, callback=None) -> list[int | None]:
    """Atomically own transitions and observer patches, including duplicate admissions.

    A stale observation returns None without reopening or patching its stream.
    Explicit occurrence keys identify one-off actions independently of text.
    The callback(session, spec_index, event_id) participates in this transaction.
    """
    now = _aware(now)
    result = []
    async with factory() as session, session.begin():
        # Deterministic order prevents two multi-stream admissions deadlocking.
        ordered = sorted(enumerate(specs), key=lambda item: _identity(item[1]["stream"]))
        by_index = {}
        for index, spec in ordered:
            stream_key = _identity(spec["stream"])
            observed = _aware(spec.get("observed_at") or now)
            phase = str(spec["phase"])
            if not phase or len(phase) > 32:
                raise ValueError("invalid notification phase")
            members = sorted(set(str(x) for x in spec.get("members") or []))
            level = int(spec.get("level") or 0)
            occurrence = key(spec["occurrence"]) if spec.get("occurrence") else None
            payload = _json(spec.get("payload") or {})
            if not isinstance(payload, dict):
                raise ValueError("notification payload must be an object")
            # prune может держать ленту и удалить ее, пока мы ждем FOR UPDATE: тогда вставка была
            # пустой (конфликт), а SELECT уже не находит строку. Вставляем снова - лента новая
            for _ in range(3):
                await _insert_once(session, Stream, dict(key=stream_key, seq=0, episode=0, phase="", members=[],
                                   level=0, observed_at=observed), ["key"])
                # The insert/no-op is a write barrier on SQLite; FOR UPDATE serializes
                # the existing stream on PostgreSQL. Both finish before network I/O.
                stream = await session.scalar(select(Stream).where(Stream.key == stream_key).with_for_update())
                if stream is not None:
                    break
            else:
                raise RuntimeError("notification stream vanished during admission")
            event = None
            if occurrence:
                event = await session.scalar(select(Event).where(Event.stream_key == stream_key,
                                                                 Event.occurrence == occurrence))
            stale = observed < _aware(stream.observed_at)
            if event is None and stale and stream.last_event_id:
                event = await session.get(Event, stream.last_event_id)
            same = (not occurrence and stream.last_event_id and phase == stream.phase
                    and set(members).issubset(stream.members or []) and level <= stream.level)
            if event is None and same:
                event = await session.get(Event, stream.last_event_id)
            if event is None:
                open_episode = bool(stream.last_event_id and stream.phase != "recovery" and phase != "recovery")
                episode = stream.episode + 1 if not stream.last_event_id or (
                    stream.phase == "recovery" and phase != "recovery") else stream.episode
                event = Event(stream_key=stream_key, seq=stream.seq + 1, episode=episode, phase=phase, level=level,
                              members=members, occurrence=occurrence, payload=payload,
                              occurred_at=observed, updated_at=now, history_recorded=False)
                session.add(event)
                await session.flush()
                stream.seq, stream.phase = event.seq, phase
                # A lower-severity update may introduce a new fact without
                # forgetting facts already announced during this open episode.
                stream.level = max(stream.level, level) if open_episode else level
                stream.episode = event.episode
                stream.members = sorted(set(stream.members or []) | set(members)) if open_episode else members
                stream.last_event_id = event.id
            elif (not stale and event.id == stream.last_event_id
                  and event.members == members and event.level == level):
                confirmed = await session.scalar(select(exists().where(
                    Delivery.event_id == event.id, Delivery.status == "confirmed")))
                if not confirmed:
                    event.payload, event.updated_at = payload, now
            if not stale:
                stream.observed_at = observed
            # A stale spec does not attach obsolete recipients to a newer event.
            if not stale:
                recipients = list(spec.get("recipients") or [])
                if phase == "recovery":
                    # A fire owned a destination even if its POST was uncertain.
                    # The same recipient must be able to receive the final state.
                    prior = (await session.execute(select(Delivery, Event).join(Event).where(
                        Event.stream_key == stream_key, Event.episode == event.episode,
                        Event.seq < event.seq))).all()
                    present = {_identity(r["key"]) for r in recipients}
                    for job, _ in prior:
                        if job.recipient_key not in present:
                            routing = {name: value for name, value in job.target.items() if name in
                                       {"type", "bot", "chat", "route", "required", "user_id"}}
                            recipients.append(dict(key=job.recipient_key, transport=job.transport_key,
                                                   target={**routing, "recovery_owned": True}))
                            present.add(job.recipient_key)
                for recipient in recipients:
                    recipient_key = _identity(recipient["key"])
                    transport_key = _identity(recipient["transport"])
                    target = _target(recipient.get("target") or {})
                    await _insert_once(session, Transport, dict(key=transport_key, failures=0,
                                       lease_owner=""), ["key"])
                    await _insert_once(session, Delivery, dict(
                        event_id=event.id, recipient_key=recipient_key, transport_key=transport_key,
                        target=target, status="pending", attempts=0, uncertain_count=0,
                        next_attempt_at=now, lease_owner="", last_error="", created_at=now,
                    ), ["event_id", "recipient_key"])
            by_index[index] = None if stale else event.id
        result = [by_index[i] for i in range(len(specs))]
        # All outbox rows exist before observers apply their patches. Preserve
        # caller order even though stream locks were acquired in sorted order.
        for index, event_id in enumerate(result):
            if event_id is not None:
                await _callback(callback, session, index, event_id)
    return result


def _view(delivery: Delivery, event: Event) -> dict:
    return dict(id=delivery.id, event_id=event.id, recipient_key=delivery.recipient_key,
                transport_key=delivery.transport_key, target=_json(delivery.target),
                payload=_json(event.payload), phase=event.phase, occurred_at=_aware(event.occurred_at),
                attempts=delivery.attempts, uncertain_count=delivery.uncertain_count,
                tg_alert_id=event.tg_alert_id, stream_key=event.stream_key, seq=event.seq, episode=event.episode,
                members=_json(event.members), level=event.level, status=delivery.status,
                message_id=delivery.message_id)


async def _expired(factory, now: datetime) -> None:
    """A process may have died after POST: lease expiry is ambiguous, never unsent."""
    async with factory() as session:
        keys = list(await session.scalars(select(Delivery.transport_key).where(
            Delivery.status == "sending", Delivery.lease_until <= now).distinct()))
    for transport_key in sorted(keys):
        async with factory() as session, session.begin():
            # Write first, also providing serialization on SQLite.
            await session.execute(update(Transport).where(Transport.key == transport_key)
                                  .values(failures=Transport.failures))
            transport = await session.get(Transport, transport_key)
            rows = list(await session.scalars(select(Delivery).where(
                Delivery.transport_key == transport_key, Delivery.status == "sending",
                Delivery.lease_until <= now).with_for_update()))
            for row in rows:
                row.uncertain_count += 1
                row.status = "attention" if row.uncertain_count >= MAX_UNCERTAIN or row.attempts >= MAX_ATTEMPTS else "uncertain"
                row.next_attempt_at = now + timedelta(seconds=UNCERTAIN_DELAY)
                row.last_error, row.lease_owner, row.lease_until = "LeaseExpired", "", None
            if rows and transport is not None:
                transport.failures += 1
                blocked = now + timedelta(seconds=UNCERTAIN_DELAY)
                transport.blocked_until = max(_aware(transport.blocked_until), blocked) if transport.blocked_until else blocked
                if transport.lease_until is None or _aware(transport.lease_until) <= now:
                    transport.lease_owner, transport.lease_until = "", None


async def due(factory, now: datetime, limit: int = 100) -> list[dict]:
    now = _aware(now)
    await _expired(factory, now)
    async with factory() as session:
        rows = (await session.execute(select(Delivery, Event).join(Event).join(
            Transport, Transport.key == Delivery.transport_key).where(
                Delivery.status.in_(_READY), Delivery.next_attempt_at <= now,
                or_(Transport.blocked_until.is_(None), Transport.blocked_until <= now),
                or_(Transport.lease_until.is_(None), Transport.lease_until <= now),
            ).order_by(Event.occurred_at, Delivery.id).limit(max(0, int(limit))))).all()
        return [_view(row, event) for row, event in rows]


async def claim(factory, ids: list[int], owner: str, now: datetime, lease_seconds: int = 120) -> list[dict]:
    """Claim one recipient/transport batch; a second worker cannot claim its POST.

    owner MUST be a fresh nonce for this POST, not a stable process identifier:
    it fences off a late HTTP response after a lease expires and gets reclaimed.
    """
    now = _aware(now)
    ids = sorted(set(int(i) for i in ids))
    if not ids:
        return []
    if not owner or len(owner) > 128 or lease_seconds < 1:
        raise ValueError("invalid notification lease")
    await _expired(factory, now)
    async with factory() as session:
        identities = set((await session.execute(select(Delivery.recipient_key, Delivery.transport_key)
                                                .where(Delivery.id.in_(ids)))).all())
    if not identities:
        return []
    if len(identities) != 1:
        raise ValueError("a claim must contain one recipient and transport")
    recipient_key, transport_key = next(iter(identities))
    until = now + timedelta(seconds=lease_seconds)
    async with factory() as session, session.begin():
        locked = await session.execute(update(Transport).where(
            Transport.key == transport_key,
            or_(Transport.lease_until.is_(None), Transport.lease_until <= now),
            or_(Transport.blocked_until.is_(None), Transport.blocked_until <= now),
        ).values(lease_owner=owner, lease_until=until))
        if not locked.rowcount:
            return []
        claimed = list((await session.scalars(update(Delivery).where(
            Delivery.id.in_(ids), Delivery.recipient_key == recipient_key,
            Delivery.transport_key == transport_key, Delivery.status.in_(_READY),
            Delivery.next_attempt_at <= now,
        ).values(status="sending", lease_owner=owner, lease_until=until,
                 attempts=Delivery.attempts + 1).returning(Delivery.id))).all())
        if not claimed:
            await session.execute(update(Transport).where(Transport.key == transport_key,
                                  Transport.lease_owner == owner).values(lease_owner="", lease_until=None))
            return []
        rows = (await session.execute(select(Delivery, Event).join(Event)
                                      .where(Delivery.id.in_(claimed)).order_by(Delivery.id))).all()
        return [_view(row, event) for row, event in rows]


def _safe_error(error: str) -> str:
    error = str(error)
    # Never retain exception text: httpx URLs and service responses may contain credentials.
    if re.fullmatch(r"HTTP[ _:]?[1-5][0-9]{2}", error):
        return error.replace(" ", "_").replace(":", "_")
    allowed = {"ReadTimeout", "ReadError", "WriteTimeout", "WriteError", "ConnectTimeout",
               "ConnectError", "PoolTimeout", "RemoteProtocolError", "LocalProtocolError",
               "TransportError", "TgRejected", "RuntimeError", "LeaseExpired", "CancelledError", "PrepareError"}
    return error if error in allowed else ("DeliveryError" if error else "")


def reason(error: str | None) -> str:
    """Причина для окна доставки: код из списка _safe_error, иначе DeliveryError.

    last_error пишется уже очищенным, но старые и чужие записи читаем так же: наружу только код.
    """
    return _safe_error(error or "")


def _retry_at(now: datetime, delay: int, retry_after) -> datetime:
    at = now + timedelta(seconds=delay)
    if isinstance(retry_after, datetime):
        return max(at, _aware(retry_after))
    if isinstance(retry_after, timedelta):
        return max(at, now + retry_after)
    if retry_after is not None:
        return max(at, now + timedelta(seconds=max(0, float(retry_after))))
    return at


async def finish(factory, ids: list[int], owner: str, status: str, now: datetime,
                 message_id: int | None = None, error: str = "", retry_after=None,
                 target_patch: dict | None = None) -> list[int]:
    """Finalize only a still-owned lease. Late responses cannot overwrite another worker."""
    if status not in {"confirmed", "rejected", "uncertain", "retry", "superseded"}:
        raise ValueError("invalid notification outcome")
    now = _aware(now)
    ids = sorted(set(int(i) for i in ids))
    if not ids:
        return []
    async with factory() as session:
        keys = set(await session.scalars(select(Delivery.transport_key).where(Delivery.id.in_(ids))))
    if len(keys) != 1:
        return []
    transport_key = next(iter(keys))
    async with factory() as session, session.begin():
        locked = await session.execute(update(Transport).where(
            Transport.key == transport_key, Transport.lease_owner == owner,
            Transport.lease_until > now,
        ).values(failures=Transport.failures))
        if not locked.rowcount:
            return []
        transport = await session.get(Transport, transport_key)
        rows = list(await session.scalars(select(Delivery).where(
            Delivery.id.in_(ids), Delivery.status == "sending", Delivery.lease_owner == owner,
            Delivery.lease_until > now,
        ).with_for_update()))
        if not rows:
            return []
        failures = transport.failures + 1 if status in {"uncertain", "retry"} else transport.failures
        delay = min(30 * (2 ** min(failures - 1, 5)), UNCERTAIN_DELAY) if failures else 0
        if status == "uncertain":
            delay = max(delay, UNCERTAIN_DELAY)
        next_attempt = _retry_at(now, delay, retry_after)
        for row in rows:
            if _PREPARE_MARKS & (row.target or {}).keys():  # дошла до исхода - метки сбоев подготовки сначала
                row.target = _fresh(row.target)
            if target_patch:
                row.target = {**row.target, **_target(target_patch)}
            row.status = "pending" if status == "retry" else status
            if status == "confirmed":
                row.effects_pending = True  # снимает alert_delivery._effects_done
            if status == "uncertain":
                row.uncertain_count += 1
            if status in {"uncertain", "retry"} and (
                    row.attempts >= MAX_ATTEMPTS or row.uncertain_count >= MAX_UNCERTAIN):
                row.status = "attention"
            row.next_attempt_at = next_attempt
            row.message_id = int(message_id) if status == "confirmed" and message_id is not None else row.message_id
            row.last_error = "" if status == "confirmed" else _safe_error(error)
            row.lease_owner, row.lease_until = "", None
        if status == "confirmed":
            transport.failures, transport.blocked_until = 0, None
        elif status in {"uncertain", "retry"}:
            transport.failures = failures
            transport.blocked_until = max(_aware(transport.blocked_until), next_attempt) if transport.blocked_until else next_attempt
        # Partial finalization must not release a lease covering unfinished rows.
        await session.flush()
        remaining = await session.scalar(select(exists().where(
            Delivery.transport_key == transport_key, Delivery.status == "sending", Delivery.lease_owner == owner)))
        if not remaining:
            transport.lease_owner, transport.lease_until = "", None
        return [row.id for row in rows]


async def defer(factory, ids: list[int], owner: str, now: datetime, until: datetime) -> list[int]:
    """Release owned jobs without POST, preserving ambiguity and its retry budget.

    Claim reserves an attempt before inspecting source policy. A snooze consumes
    no network attempt, so undo that reservation while retaining the circuit.
    An expired own lease is safe here: both owners/status still fence a reclaim.
    """
    return await _release(factory, ids, owner, now, until, failed=False)


async def fail_prepare(factory, ids: list[int], owner: str, now: datetime, delay: int = PREPARE_DELAY, *,
                       count: bool = True, solo: bool = False) -> list[int]:
    """Подготовка POST упала (рендер, политика, получатель): попытки не было, как в defer.

    Но с отсрочкой и счетчиком подряд в target: строка, которая падает всегда, не стоит вечно в
    голове due и не держит канал получателя. После MAX_PREPARE подряд - attention с причиной
    PrepareError, как после MAX_UNCERTAIN: оператор видит ее в окне доставки. Транспорт не
    блокируется, соседние строки идут.

    count=False - сбой не строки (база, сеть, пачка целиком): только отсрочка. solo=True - пачка
    упала, а чья строка виновата, неизвестно: до SOLO_WINDOW drain шлет эти строки по одной, и
    счетчик растет только у той, что падает сама. Метки снимают finish и retry.
    """
    now = _aware(now)
    return await _release(factory, ids, owner, now, now + timedelta(seconds=delay), failed=True,
                          count=count, solo=solo)


def _fresh(target: dict) -> dict:
    """target без меток сбоев подготовки (prepare_failures, solo_until)."""
    return {name: value for name, value in (target or {}).items() if name not in _PREPARE_MARKS}


async def _release(factory, ids: list[int], owner: str, now: datetime, until: datetime, *, failed: bool,
                   count: bool = True, solo: bool = False) -> list[int]:
    now, until = _aware(now), _aware(until)
    ids = sorted(set(int(value) for value in ids))
    if not ids:
        return []
    async with factory() as session:
        keys = set(await session.scalars(select(Delivery.transport_key).where(Delivery.id.in_(ids))))
    if len(keys) != 1:
        return []
    transport_key = next(iter(keys))
    async with factory() as session, session.begin():
        locked = await session.execute(update(Transport).where(
            Transport.key == transport_key, Transport.lease_owner == owner,
        ).values(failures=Transport.failures))
        if not locked.rowcount:
            return []
        rows = list(await session.scalars(select(Delivery).where(
            Delivery.id.in_(ids), Delivery.status == "sending", Delivery.lease_owner == owner,
        ).order_by(Delivery.id).with_for_update()))
        for row in rows:
            target = dict(row.target or {})
            if not failed:
                target.pop("prepare_failures", None)  # подготовка удалась (defer) - счетчик подряд сначала
            elif count:
                target["prepare_failures"] = int(target.get("prepare_failures") or 0) + 1
            if solo:
                target["solo_until"] = (now + timedelta(seconds=SOLO_WINDOW)).isoformat()
            if target != row.target:
                row.target = target
            row.status = "uncertain" if row.uncertain_count else "pending"
            if failed:
                row.last_error = "PrepareError"
                if count and target["prepare_failures"] >= MAX_PREPARE:
                    row.status = "attention"
            row.attempts = max(0, row.attempts - 1)
            row.next_attempt_at = max(_aware(row.next_attempt_at), until, now)
            row.lease_owner, row.lease_until = "", None
        await session.flush()
        remaining = await session.scalar(select(exists().where(
            Delivery.transport_key == transport_key, Delivery.status == "sending", Delivery.lease_owner == owner)))
        if not remaining:
            await session.execute(update(Transport).where(Transport.key == transport_key,
                                  Transport.lease_owner == owner).values(lease_owner="", lease_until=None))
        return [row.id for row in rows]


async def retry(factory, ids: list[int], now: datetime, *, acknowledge_uncertain=None) -> list[int]:
    """Explicit operator retry. Keep reviewed, confirmed, superseded and active outcomes intact."""
    now = _aware(now)
    await _expired(factory, now)
    eligible = (Delivery.id.in_(ids), Delivery.status.in_(("pending", "uncertain", "attention", "rejected")),
                Delivery.reviewed_at.is_(None))
    if acknowledge_uncertain is False:
        eligible += (Delivery.uncertain_count == 0,)
    async with factory() as session:
        keys = list(await session.scalars(select(Delivery.transport_key).where(*eligible).distinct()))
    async with factory() as session, session.begin():
        # An explicit retry permits one new probe, while preserving active leases.
        # Take transport locks before recipient rows, as claim/finish do.
        for transport_key in sorted(keys):
            await session.execute(update(Transport).where(Transport.key == transport_key)
                                  .values(failures=Transport.failures))
            target = select(Delivery.id).where(*eligible, Delivery.transport_key == transport_key).with_for_update()
            if await session.scalar(target) is not None:
                await session.execute(update(Transport).where(Transport.key == transport_key)
                                      .values(failures=0, blocked_until=now))
        statement = update(Delivery).where(*eligible)
        queued = list(await session.scalars(statement.values(status="pending", attempts=0, uncertain_count=0, next_attempt_at=now,
                      lease_owner="", lease_until=None, last_error="").returning(Delivery.id)))
        # ручной повтор - с чистого листа и для сбоев подготовки, иначе одна неудача снова attention
        for row in await session.scalars(select(Delivery).where(Delivery.id.in_(queued))):
            if _PREPARE_MARKS & (row.target or {}).keys():
                row.target = _fresh(row.target)
        return queued


async def set_tg_alert(factory, event_id: int, aid: int) -> int:
    """Reuse the callback row embedded in the original message, including cached delivery."""
    async with factory() as session, session.begin():
        await session.execute(update(Event).where(Event.id == event_id, Event.tg_alert_id.is_(None))
                              .values(tg_alert_id=aid))
        result = await session.scalar(select(Event.tg_alert_id).where(Event.id == event_id))
        if result is None:
            raise ValueError("unknown notification event")
        return result


async def mark_history(factory, event_ids: list[int], callback=None) -> list[int]:
    """Mark confirmed events once, optionally writing history in the SAME transaction.

    callback(session, event_ids) must create history rows here, not after returning:
    a later transaction would leave a crash window between the marker and history.
    """
    async with factory() as session, session.begin():
        ids = list(await session.scalars(update(Event).where(
            Event.id.in_(event_ids), Event.history_recorded.is_(False),
            exists().where(Delivery.event_id == Event.id, Delivery.status == "confirmed"),
        ).values(history_recorded=True).returning(Event.id)))
        if ids:
            await _callback(callback, session, ids)
        return ids


async def load_events(factory, ids: list[int]) -> dict[int, dict]:
    """Detached event metadata for the worker; reads do not imply confirmation."""
    async with factory() as session:
        rows = list(await session.scalars(select(Event).where(Event.id.in_(ids))))
        return {row.id: dict(id=row.id, stream_key=row.stream_key, seq=row.seq, episode=row.episode,
                            phase=row.phase, level=row.level, members=_json(row.members),
                            occurrence=row.occurrence, payload=_json(row.payload),
                            occurred_at=_aware(row.occurred_at), updated_at=_aware(row.updated_at),
                            history_recorded=row.history_recorded, tg_alert_id=row.tg_alert_id) for row in rows}


def _settled(row, stale: datetime):
    """Доставка, которую уже никто не тронет: ни drain, ни повтор, ни окно "Доставка".

    rejected в окне - проблема с кнопкой повтора, отметки просмотра у нее нет. Закрытой она
    считается, когда по тому же событию подтверждена копия в основной чат (fallback). Все, что
    требует решения (attention без просмотра, rejected), закрывается само через год (stale).
    """
    fallback = aliased(Delivery)
    return or_(and_(row.status == "confirmed", row.effects_pending == False),  # noqa: E712
               row.status == "superseded",
               and_(row.status == "attention", row.reviewed_at.is_not(None)),
               and_(row.status == "rejected", exists().where(
                   fallback.event_id == row.event_id, fallback.status == "confirmed",
                   fallback.target["route"].as_string() == "fallback")),
               and_(row.status.in_(("attention", "rejected")), row.created_at < stale, row.next_attempt_at < stale))


def _prunable(cutoff: datetime, stale: datetime):
    """Условие на событие: старое, его доставки закрыты и эпизод больше ничего не ждет.

    Последнее событие ленты (last_event_id) отсюда не исключено: его удаляют только вместе
    с самой лентой, это решает prune. Ссылки covered_events/superseded_by, fan-out отбоя
    (admit) и кнопки эпизода (_resolve_buttons) живут внутри одного (stream_key, episode),
    поэтому тревогу или отбой удаляем, только когда в эпизоде не осталось незакрытых доставок.
    Разовое событие (phase event) эпизод не закрывает никогда: для него смотрим только его
    собственные доставки. Это и event внутри открытого эпизода смешанной ленты (например
    server:*:backup_growth): оно уходит по своим доставкам, даже если отбой эпизода еще не
    отправлен. Тогда из истории этого отбоя может пропасть строка "Событие сообщено вместе
    с восстановлением" для такого event (covered_events ссылается на удаленное, _history
    его пропускает), сам отбой и его получатели не меняются.
    """
    own, other, job = aliased(Delivery), aliased(Event), aliased(Delivery)
    # лента - скалярными подзапросами, а не join: это всегда поиск по ключу. С join планировщик
    # после autoanalyze брал seq scan лент на каждое событие (1M событий: 8 с на пачку)
    episode = select(Stream.episode).where(Stream.key == Event.stream_key).scalar_subquery()
    phase = select(Stream.phase).where(Stream.key == Event.stream_key).scalar_subquery()
    return and_(
        Event.occurred_at < cutoff, Event.updated_at < cutoff,
        # открытый эпизод хранит свои тревоги: по ним отбой находит получателей (admit, recovery)
        or_(Event.phase == "event", phase == "recovery", Event.episode != episode),
        ~exists().where(Delivery.event_id == Event.id, or_(
            Delivery.created_at >= cutoff, Delivery.next_attempt_at >= cutoff, Delivery.reviewed_at >= cutoff)),
        or_(and_(Event.phase == "event", ~exists().where(own.event_id == Event.id, ~_settled(own, stale))),
            and_(Event.phase != "event", ~exists().where(
                job.event_id == other.id, other.stream_key == Event.stream_key, other.episode == Event.episode,
                ~_settled(job, stale)))),
    )


# где остановилась прошлая чистка, по фабрике сессий: следующая продолжает с этого места, а не
# проходит заново все, что хранится (открытые эпизоды, требующее решения). После конца - с начала
_resume: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def prune(factory, cutoff: datetime, stale: datetime, *, batch: int = 500, budget: float = 10.0) -> dict:
    """Удалить старые закрытые события с доставками и закрытые ленты без новых наблюдений.

    Каждая пачка - своя короткая транзакция. Ленты пачки блокируются в порядке ключа, как
    в admit, и только потом условия проверяются заново: новое событие или получатель
    появляются только под блокировкой ленты. Закрытую доставку может снова открыть повтор
    оператора (retry: rejected с копией в основной чат, attention старше года) без блокировки
    ленты, поэтому доставки удаляются только закрытые, и если удалено меньше, чем их у этих
    событий, пачка откатывается целиком: события останутся до следующего прохода.
    Лента удаляется вместе с последним событием, когда она закрыта (recovery) или разовая
    (event), давно не наблюдалась и других событий у нее не осталось. Новая пачка не
    начинается после budget секунд: остальное - в следующий запуск. Повторный запуск ничего
    не меняет. Возвращает число удаленных строк.
    """
    cutoff, stale = _aware(cutoff), _aware(stale)
    done = dict(events=0, deliveries=0, streams=0)
    after = _resume.pop(factory, 0)
    deadline = _monotonic() + max(0.0, float(budget))
    while True:
        if _monotonic() >= deadline:
            _resume[factory] = after
            break
        async with factory() as session:
            rows = (await session.execute(select(Event.id, Event.stream_key).where(
                Event.id > after, _prunable(cutoff, stale)).order_by(Event.id).limit(max(1, int(batch))))).all()
        if not rows:
            break
        after = rows[-1][0]
        try:
            removed = await _prune_batch(factory, rows, cutoff, stale)
        except _Reopened:
            continue
        for name, value in removed.items():
            done[name] += value
    return done


class _Reopened(Exception):
    """Доставку пачки открыли заново (retry) между проверкой и удалением."""


async def _drop_deliveries(session, event_ids: list[int], stale: datetime) -> int:
    expected = await session.scalar(select(func.count()).select_from(Delivery).where(Delivery.event_id.in_(event_ids)))
    deleted = (await session.execute(delete(Delivery).where(
        Delivery.event_id.in_(event_ids), _settled(Delivery, stale)))).rowcount
    if deleted != expected:
        raise _Reopened
    return deleted


async def _prune_batch(factory, rows, cutoff: datetime, stale: datetime) -> dict:
    done = dict(events=0, deliveries=0, streams=0)
    keys = sorted({key for _, key in rows})
    async with factory() as session, session.begin():
        if session.bind.dialect.name == "sqlite":
            # запись первой: на SQLite это блокировка базы на всю пачку
            await session.execute(update(Stream).where(Stream.key == keys[0]).values(seq=Stream.seq))
        streams = {row.key: row for row in await session.scalars(
            select(Stream).where(Stream.key.in_(keys)).order_by(Stream.key).with_for_update())}
        ids = list(await session.scalars(select(Event.id).where(
            Event.id.in_([event_id for event_id, _ in rows]), _prunable(cutoff, stale))))
        last = {stream.last_event_id for stream in streams.values()}
        events = [event_id for event_id in ids if event_id not in last]
        closing = {stream.key: stream.last_event_id for stream in streams.values()
                   if stream.last_event_id in ids and stream.phase in ("recovery", "event")
                   and _aware(stream.observed_at) < cutoff}
        if events:
            done["deliveries"] += await _drop_deliveries(session, events, stale)
            done["events"] += (await session.execute(delete(Event).where(Event.id.in_(events)))).rowcount
        if closing:
            # остались события, которые держит эпизод или незакрытая доставка: лента живет
            busy = set(await session.scalars(select(Event.stream_key).where(
                Event.stream_key.in_(list(closing)), Event.id.not_in(list(closing.values()))).distinct()))
            closing = {key: last_id for key, last_id in closing.items() if key not in busy}
        if closing:
            done["deliveries"] += await _drop_deliveries(session, list(closing.values()), stale)
            done["events"] += (await session.execute(delete(Event).where(
                Event.id.in_(list(closing.values()))))).rowcount
            done["streams"] += (await session.execute(delete(Stream).where(
                Stream.key.in_(list(closing))))).rowcount
    return done


async def deliveries(factory, event_ids: list[int]) -> list[dict]:
    """All recipient outcomes, including uncertainty and permanent rejection."""
    async with factory() as session:
        rows = (await session.execute(select(Delivery, Event).join(Event)
                                      .where(Delivery.event_id.in_(event_ids)).order_by(Delivery.id))).all()
        return [{**_view(row, event), "next_attempt_at": _aware(row.next_attempt_at),
                 "lease_owner": row.lease_owner,
                 "lease_until": _aware(row.lease_until) if row.lease_until else None,
                 "last_error": row.last_error, "created_at": _aware(row.created_at)} for row, event in rows]
