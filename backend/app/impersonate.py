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
    от имени админа с пометкой as_user (см. audit.record).

Кто сейчас действует, хранит contextvar: его ставит get_current_user на каждый запрос, а
ResetMiddleware сбрасывает в начале запроса (в тестах запросы идут в одной задаче asyncio и
иначе пометка перетекала бы в следующий запрос)."""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

IMP_TTL_MINUTES = 60

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
