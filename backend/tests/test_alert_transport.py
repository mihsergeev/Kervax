"""Транспорт алертов: длинные и битые сообщения, отказ Telegram навсегда, умерший вебхук,
недоступный Telegram, подделка строк алерта именем от агента, разметка в вебхуке."""
import html
import logging

import httpx
import pytest

from app import alerts, settings_store, tg_fmt


class _Resp:
    def __init__(self, data, code=200):
        self._data, self.status_code = data, code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok():
    FakeHTTP.next_id += 1
    return _Resp({"ok": True, "result": {"message_id": FakeHTTP.next_id}})


class FakeHTTP:
    """Подмена httpx.AsyncClient: answer(url, json) решает, что ответить (или что бросить)."""
    calls: list = []
    made = 0
    next_id = 100
    answer = None

    def __init__(self, *a, **k):
        FakeHTTP.made += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        FakeHTTP.calls.append((url, dict(json or {})))
        return FakeHTTP.answer(url, json) if FakeHTTP.answer else _ok()

    @classmethod
    def tg(cls) -> list[dict]:
        return [j for u, j in cls.calls if u.endswith("/sendMessage")]

    @classmethod
    def hook(cls) -> list[dict]:
        return [j for u, j in cls.calls if u.startswith("https://hook.example.com")]


@pytest.fixture
def http(monkeypatch):
    FakeHTTP.calls, FakeHTTP.made, FakeHTTP.next_id, FakeHTTP.answer = [], 0, 100, None
    monkeypatch.setattr("app.alerts.httpx.AsyncClient", FakeHTTP)
    return FakeHTTP


def _cfg(**kw):
    return {"telegram_token": "fake-token-example", "telegram_chat": "-100", "telegram_api": "",
            "webhook": "", "tg_buttons": False, "flood_threshold": 6, "routes": {}, **kw}


def _msg(name="node-a", text=""):
    return alerts.Msg(text or f"🔴 <b>{name}</b>: диск заполнен", "servers", "", "disk", name, ref="srv:1")


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


# ---- длина и разметка (веб 6) ----

def test_clip_keeps_tags_and_entities_whole():
    body = "".join(f"строка {i}: a &amp; b <i>x</i>\n" for i in range(400))
    text = f"<b>{body}</b><pre>" + "лог &lt;tail&gt; " * 400 + "</pre>"
    out = alerts._clip(text, True)  # noqa: SLF001
    assert alerts._u16(out) <= 4096  # noqa: SLF001
    assert out.endswith("...</b>")  # отрезано по переносу строки, <b> закрыт
    tail = out[:-len("...</b>")]
    assert tail.endswith("</i>") and tail.count("<i>") == tail.count("</i>")
    # посреди тега и сущности не режется
    cut = alerts._clip("<b>" + "x" * 3990 + '<a href="https://example.com/">y</a> &amp; z' * 5, True)  # noqa: SLF001
    assert "<a href=\"https" not in cut.rsplit("...", 1)[1] and not cut.split("...")[0].endswith("&am")
    assert cut.endswith("</b>")
    assert alerts._clip("коротко", True) == "коротко"  # noqa: SLF001


def test_clip_counts_emoji_as_two():
    out = alerts._clip("🔴" * 3000, False)  # noqa: SLF001
    assert alerts._u16(out) <= 4000 and out.endswith("...")  # noqa: SLF001


async def test_long_text_is_clipped_and_sent(http):
    text = "🔴 <b>node-a</b>\n" + "\n".join(f"контейнер web-{i} &amp; db" for i in range(1000))
    assert await alerts.dispatch(_cfg(), True, [_msg(text=text)], 0, parse_mode="HTML")
    sent = http.tg()
    assert len(sent) == 1 and sent[0]["parse_mode"] == "HTML"
    assert len(sent[0]["text"]) <= 4000 and sent[0]["text"].endswith("...")


async def test_bad_markup_is_resent_as_plain_text(http):
    def answer(url, j):
        if j.get("parse_mode"):
            return _Resp({"ok": False, "description": "Bad Request: can't parse entities"}, 400)
        return _ok()

    http.answer = answer
    markup = {"inline_keyboard": [[{"text": "взял", "callback_data": "kx:1:ak"}]]}
    mid = await alerts.tg_send("", "fake-token-example", "-100", "🔴 <b>node-a</b> &lt;1&gt; &amp; <i>x",
                               "HTML", markup)
    assert mid == 101
    first, second = http.tg()
    assert first["parse_mode"] == "HTML"
    assert "parse_mode" not in second and second["reply_markup"] == markup
    assert second["text"] == "🔴 node-a <1> & x"


async def test_rejected_twice_is_not_repeated_and_logged(http, caplog):
    """Telegram отказал и обычному тексту: повтор этого сообщения не поможет, а повтор всей пачки
    из-за него раньше слал уже доставленное каждый тик. Ошибка - в журнале (ERROR)."""
    def answer(url, j):
        if "node-b" in j["text"]:
            return _Resp({"ok": False, "description": "Bad Request: message is too long"}, 400)
        return _ok()

    http.answer = answer
    with caplog.at_level(logging.ERROR, logger="kervax.alerts"):
        ok = await alerts.dispatch(_cfg(), True, [_msg("node-a"), _msg("node-b")], 0, parse_mode="HTML")
    assert ok is True
    assert len(http.tg()) == 3  # node-a, node-b с разметкой и node-b обычным текстом
    errs = [r for r in caplog.records if r.levelno == logging.ERROR and r.name == "kervax.alerts"]
    assert errs and "message is too long" in errs[0].getMessage() and "node-b" in errs[0].getMessage()


async def test_rejected_error_is_visible_in_send_alert(http):
    """Кнопка "проверить" в настройках видит отказ: chat not found не выдается за успех."""
    http.answer = lambda url, j: _Resp({"ok": False, "description": "Bad Request: chat not found"}, 400)
    errors = await alerts.send_alert(_cfg(), "проверка")
    assert errors == ["Telegram: HTTP 400: Bad Request: chat not found"] and errors.rejected == 1


@pytest.mark.parametrize("fail", ["connect", "timeout", "502", "429"])
async def test_network_errors_are_retried(http, fail):
    def answer(url, j):
        if fail == "connect":
            raise httpx.ConnectError("refused")
        if fail == "timeout":
            raise httpx.ReadTimeout("slow")
        return _Resp({"ok": False, "description": "Too Many Requests: retry after 5"}, int(fail))

    http.answer = answer
    assert await alerts.dispatch(_cfg(), True, [_msg()], 0, parse_mode="HTML") is False


async def test_server_error_text_has_no_token(http):
    http.answer = lambda url, j: _Resp({"ok": False, "description": "Bad Gateway"}, 502)
    errors = await alerts.send_alert(_cfg(), "x")
    assert errors == ["Telegram: HTTP 502: Bad Gateway"] and errors.rejected == 0
    assert "fake-token-example" not in errors[0]


# ---- вебхук против Telegram (бэкенд 9) ----

async def test_dead_webhook_does_not_repeat_telegram(client, http):
    f = _factory(client)

    def answer(url, j):
        if url.startswith("https://hook.example.com"):
            return _Resp({}, 500)
        return _ok()

    http.answer = answer
    cfg = _cfg(webhook="https://hook.example.com/in/fake-token-example")
    assert await alerts.dispatch(cfg, True, [_msg()], 0, parse_mode="HTML", session_factory=f)
    async with f() as s:
        state = await settings_store.get_routes_state(s)
    err = state[alerts.WEBHOOK_STATE_KEY]["err"]
    assert err == "HTTP 500" and "fake-token-example" not in err
    # вебхук ожил - ошибка уходит
    http.answer = None
    assert await alerts.dispatch(cfg, True, [_msg()], 0, parse_mode="HTML", session_factory=f)
    async with f() as s:
        assert alerts.WEBHOOK_STATE_KEY not in await settings_store.get_routes_state(s)


async def test_both_channels_down_is_retried(http):
    def answer(url, j):
        raise httpx.ConnectError("refused")

    http.answer = answer
    cfg = _cfg(webhook="https://hook.example.com/in")
    assert await alerts.dispatch(cfg, True, [_msg()], 0, parse_mode="HTML") is False


async def test_webhook_alone_still_retried(http):
    http.answer = lambda url, j: _Resp({}, 503)
    cfg = _cfg(telegram_token="", telegram_chat="", webhook="https://hook.example.com/in")
    assert await alerts.dispatch(cfg, True, [_msg()], 0, parse_mode="HTML") is False


# ---- недоступный Telegram (бэкенд 8) ----

async def test_telegram_timeout_skips_the_rest(http):
    def answer(url, j):
        raise httpx.ConnectTimeout("timed out")

    http.answer = answer
    msgs = [_msg(f"node-{c}") for c in "abcd"]
    assert await alerts.dispatch(_cfg(), True, msgs, 0, parse_mode="HTML") is False
    assert len(http.tg()) == 1  # остальные три не пытались, повтор на следующем тике
    assert http.made == 1  # один клиент на всю пачку


async def test_one_client_per_dispatch(http):
    cfg = _cfg(webhook="https://hook.example.com/in")
    # разные тексты, чтобы не склеились штормом
    msgs = [alerts.Msg(f"🔴 <b>{n}</b>: диск {90 + i}%", "servers", "", "disk", n, ref=f"srv:{i}")
            for i, n in enumerate(("node-a", "node-b", "node-c"))]
    assert await alerts.dispatch(cfg, True, msgs, 0, parse_mode="HTML")
    assert len(http.tg()) == 3 and len(http.hook()) == 3 and http.made == 1


# ---- значения от агента (веб 13) ----

def test_agent_values_cannot_forge_lines():
    evil = "web\n✅ db-a: снова на связи"
    assert tg_fmt.names([evil, "db\r\n"]) == "web ✅ db-a: снова на связи, db"
    text = tg_fmt.render("🔴🐳", tg_fmt.link(evil, "https://example.com/s/1"), "Контейнер\nупал",
                         [tg_fmt.fact("Контейнер", evil), "под\tweb\x00-1"])
    assert len(text.split("\n")) == 4  # имя, что случилось, две строки конкретики
    assert "\t" not in text and "\x00" not in text
    line = tg_fmt.one_line("🔴🐳", "node-a", "упал", [evil])
    assert "\n" not in line
    assert "\n" not in tg_fmt.recovery("node-a", "снова\nна связи")
    assert tg_fmt.flat("a\u2028b\u202ec") == "a b c"
    # clean для целых текстов переносы оставляет
    assert tg_fmt.clean("строка 1\nстрока 2") == "строка 1\nстрока 2"


def test_agent_values_are_clipped():
    long = "m" * 500
    assert len(tg_fmt.names([long])) == tg_fmt.VALUE_MAX and tg_fmt.names([long]).endswith("...")
    assert len(tg_fmt.clip(long, 50)) == 50
    assert tg_fmt.clip("коротко") == "коротко"
    assert len(tg_fmt.fact("Mount", long * 3)) <= len("Mount: ") + tg_fmt.FACT_LEN
    assert tg_fmt.field("  /data\n ") == "/data"


# ---- разметка в вебхуке (веб 17c) ----

async def test_webhook_gets_plain_escaped_text(http):
    name = "[жми](https://example.com/x) @everyone <@123>"
    text = f'🔴 <a href="https://example.com/s/1">node-a</a>: контейнер {html.escape(name)} &amp; db'
    cfg = _cfg(telegram_token="", telegram_chat="", webhook="https://hook.example.com/in")
    assert await alerts.dispatch(cfg, True, [_msg(text=text)], 0, parse_mode="HTML")
    (body,) = http.hook()
    assert body["text"] == ("🔴 node-a: контейнер \\[жми\\]\\(https://example.com/x\\) @\u200beveryone "
                            "\\<@\u200b123\\> & db")
    # без parse_mode текст не HTML: теги не снимаются, но разметка экранируется все равно
    assert alerts.webhook_text("a<b> [x]") == "a\\<b\\> \\[x\\]"


# ---- точные ключи мьюта (для кнопки "заглушить") ----

def test_msg_mutes_survive_tail_and_storm():
    m = alerts.Msg("🔴 node-a: диск", "servers", "", "disk", "node-a", ref="srv:1", mutes=["disk:/data"])
    assert alerts.Msg("x").mutes == []
    assert m.tail("\nеще").mutes == ["disk:/data"]
    short = alerts.Msg("🔴 node-a: диск", kind="disk", target="node-a", full="полный", mutes=["disk:/"])
    assert short.tail("\nхвост", "\nкоротко").mutes == ["disk:/"]
    own = (("node-a", ["/data"]), ("node-b", ["/data", "/var"]), ("node-c", []))
    parts = [alerts.Msg(f"🔴 {n}: диск заполнен", "servers", "", "disk", n, ref=f"srv:{i}",
                        mutes=[f"disk:{p}" for p in mts])
             for i, (n, mts) in enumerate(own, start=1)]
    (storm,) = alerts.merge_storm(parts)
    assert storm.mutes == ["disk:/data", "disk:/var"]
    assert storm.tail("\n📝 заметка").mutes == storm.mutes
