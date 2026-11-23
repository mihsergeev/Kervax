"""Вход админа под чужой учеткой, как impersonation в GitLab: посмотреть панель глазами
пользователя (какие разделы, группы, серверы и кнопки ему видны) без его пароля и 2FA.

Как устроено:
  * начать может только админ и только под не-админом - у другого админа те же права, смотреть
    там нечего, а действовать от его имени незачем. Свой пароль еще раз не спрашивается (было в
    1.4.307): админ и так может все, а здесь получает меньше - только просмотр на час;
  * выдается отдельный токен на IMP_TTL_MINUTES со ссылкой на строку impersonations: токен
    админа не трогается и остается у браузера, "Вернуться к себе" закрывает строку и токен
    входа сразу перестает действовать, не дожидаясь срока;
  * весь сервер отвечает по правам пользователя (get_current_user отдает его учетку), а любые
    изменения запрещены: режим "посмотреть, как видит". Менять от чужого имени - значит потом
    разбираться, кто что сделал, а для правок у админа есть своя учетка;
  * в журнал действий пишется начало и конец, а все, что попадет в журнал за время входа, -
    от имени админа с пометкой as_user (см. audit.record);
  * вход живет, пока жива вкладка, где его начали: только у нее есть токен админа, и раз в
    минуту она подтверждает этим токеном, что жива (POST .../alive). Сервер продлевает строку на
    IMP_IDLE_SECONDS, но не дальше IMP_TTL_MINUTES от начала. Закрытая вкладка (уход со страницы
    шлет leaving) оставляет входу IMP_LEAVE_GRACE_SECONDS - перезагрузке этого хватает, чтобы
    подтвердить снова; вкладка, убитая без pagehide, - не больше IMP_IDLE_SECONDS. Раньше токен
    входа в соседних вкладках жил до конца часа, а вернуться к себе было уже неоткуда.
    Срок в токене (exp) - это час от начала, а expires_at строки - до какого времени вход жив.

Кто сейчас действует, хранит contextvar: его ставит get_current_user на каждый запрос, а
ResetMiddleware сбрасывает в начале запроса (в тестах запросы идут в одной задаче asyncio и
иначе пометка перетекала бы в следующий запрос)."""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

IMP_TTL_MINUTES = 60
# Подтверждение раз в минуту (фронт) и запас на троттлинг таймеров фоновой вкладки: Chrome будит
# скрытую вкладку не чаще раза в минуту, поэтому окно - несколько пропущенных подтверждений
IMP_IDLE_SECONDS = 300
IMP_LEAVE_GRACE_SECONDS = 30

# причины конца входа в журнале действий
EXPIRED = "истек срок"
TAB_GONE = "вкладка входа закрыта или не на связи"

# Что разрешено менять во время входа под чужой учеткой: ошибки интерфейса и "Сообщить о
# проблеме" (запись в журнал ошибок панели с пометкой, кто на самом деле смотрел). Свой пароль,
# 2FA и Telegram пользователя - нет, хотя самому пользователю они открыты
ALLOWED_WRITES = ("/api/panel-errors/front", "/api/panel-errors/report")

READ_ONLY = "Вы смотрите панель глазами {user}: менять ничего нельзя. Чтобы что-то поменять, вернитесь к себе"


@dataclass(frozen=True)
class Acting:
    imp_id: int
    admin: str
    target: str
    expires_at: datetime


_acting: ContextVar[Acting | None] = ContextVar("kervax_impersonation", default=None)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hard_cap(row) -> datetime:
    """Дальше этого вход не продлевается: IMP_TTL_MINUTES от начала, как exp его токена."""
    return _aware(row.started_at) + timedelta(minutes=IMP_TTL_MINUTES)


def end_reason(row, now: datetime, why: str) -> str:
    """Причина конца для журнала: why, пока вход жив, иначе - истек час или вкладка входа
    перестала подтверждать (закрыта, спит, нет сети). Запас - на часы базы и приложения."""
    exp = _aware(row.expires_at)
    if exp > now:
        return why
    return EXPIRED if exp >= hard_cap(row) - timedelta(seconds=60) else TAB_GONE


async def close(session, row, why: str, now: datetime) -> bool:
    """Закрывает вход и пишет конец в журнал действий - один раз, даже если его закрывают сразу
    "Вернуться к себе", новый вход и обход планировщика. Истекший закрыт временем истечения."""
    from sqlalchemy import update

    from app import audit
    from app.models import Impersonation

    reason = end_reason(row, now, why)
    ended = now if _aware(row.expires_at) > now else _aware(row.expires_at)
    res = await session.execute(update(Impersonation).where(
        Impersonation.id == row.id, Impersonation.ended_at.is_(None)).values(ended_at=ended))
    await session.commit()
    if not res.rowcount:
        return False
    await audit.record(session, row.admin, "impersonate_stop", row.target, reason)
    return True


async def close_lapsed(session_factory) -> int:
    """Тик планировщика: входы, которые никто не закрыл (вкладку входа закрыли, истек час),
    закрываются с записью в журнал, не дожидаясь, пока админ вернется или начнет новый вход."""
    from sqlalchemy import select

    from app.models import Impersonation

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        rows = [r for r in await session.scalars(select(Impersonation).where(Impersonation.ended_at.is_(None)))
                if _aware(r.expires_at) <= now]
        n = 0
        for r in rows:
            n += await close(session, r, EXPIRED, now)
    return n


def set_acting(a: Acting | None) -> None:
    _acting.set(a)


def acting() -> Acting | None:
    return _acting.get()


def label(username: str) -> str:
    """Имя для журнала ошибок и лимитов: "админ как пользователь" во время чужого входа,
    иначе само имя. Так ошибка, пойманная админом, не повиснет на пользователе."""
    a = _acting.get()
    if a is not None and a.target == username:
        return f"{a.admin} как {a.target}"[:64]
    return username


def who_of(payload: dict | None) -> str:
    """То же по payload токена (middleware журнала ошибок видит только заголовок)."""
    sub = str((payload or {}).get("sub") or "")
    imp = (payload or {}).get("imp")
    return (f"{imp} как {sub}" if imp else sub)[:64]


class ResetMiddleware:
    """Сбрасывает пометку в начале каждого запроса (см. docstring модуля)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            _acting.set(None)
        await self.app(scope, receive, send)
