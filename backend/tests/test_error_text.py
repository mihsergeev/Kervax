"""Сетевые ошибки должны читаться человеком, а не только инженером."""

import pytest

from app.checks import http_status_text, humanize_error

CASES = [
    ("[Errno -2] Name or service not known", "DNS"),
    ("[Errno 111] Connection refused", "порт закрыт"),
    ("All connection attempts failed", "подключиться"),
    ("ConnectTimeout", "не ответил"),
    ("ReadTimeout", "не прислал ответ"),
    ("Server disconnected without sending a response.", "разорвал соединение"),
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired", "истёк"),
    ("hostname 'a.example' doesn't match 'b.example'", "другое имя"),
    ("Too many redirects", "перенаправлений"),
]


@pytest.mark.parametrize("raw,expect", CASES)
def test_known_errors_are_explained(raw, expect):
    out = humanize_error(raw)
    assert expect in out
    # техническую суть оставляем в скобках: по ней ищут в логах и гуглят
    assert raw[:20] in out


def test_unknown_error_is_left_as_is():
    # выдумывать причину нельзя: непонятный текст лучше уверенного вранья
    raw = "Что-то совершенно новое от библиотеки"
    assert humanize_error(raw) == raw


def test_message_stays_short():
    assert len(humanize_error("ReadTimeout " + "x" * 5000)) <= 500


HTTP_CASES = [
    (403, "доступ запрещён"),
    (404, "страница не найдена"),
    (429, "слишком много запросов"),
    (500, "внутренняя ошибка сервера"),
    (502, "плохой ответ от бэкенда"),
    (503, "сервис недоступен"),
    (504, "бэкенд не ответил вовремя"),
    # коды Cloudflare нестандартные, но встречаются постоянно
    (521, "сервер за прокси не отвечает"),
    (522, "прокси не смог подключиться"),
]


@pytest.mark.parametrize("code,expect", HTTP_CASES)
def test_http_codes_are_explained(code, expect):
    out = http_status_text(code)
    assert expect in out
    # сам код остаётся: по нему сверяются с логом веб-сервера
    assert f"HTTP {code}" in out


def test_unknown_http_code_is_plain():
    # выдумывать значение неизвестного кода не станем
    assert http_status_text(599) == "HTTP 599"
