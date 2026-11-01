"""All notification paths share bounded text while history and delivery scope stay complete."""
import html
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from app import alerts, collector, notification_text, settings_store, srv_short
from app.models import AlertEvent, Server, User
from tests.test_known import _panel

BASE = 'https://panel.example.test/?server=1'


class RichCheck(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.links = [], '', []
        self.feed(text)
        assert not self.stack

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs)['href'])

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, value):
        self.text += value


def bounded(text):
    parsed = RichCheck(str(text))
    assert len(parsed.text) <= 480
    assert len(parsed.text.splitlines()) <= 4
    assert all(len(line) <= 150 for line in parsed.text.splitlines())
    return parsed


def test_short_text_is_unchanged():
    text = f'🔴 <b><a href="{BASE}">node-a</a></b>\nНе отвечает\nОшибка подключения'
    assert notification_text.brief(text, True) == text


@pytest.mark.parametrize('entity', ['&lt;', '&amp;', '&#128680;'])
def test_limits_count_visible_text_and_keep_long_links_and_nested_markup(entity):
    url = BASE+'&filter='+('x'*1200)
    text = f'<b><a href="{html.escape(url, quote=True)}"><i>{entity*1000}</i></a></b>\nСбой диска'
    parsed = bounded(notification_text.brief(text, True))
    assert url in parsed.links and 'Сбой диска' in parsed.text


def test_link_after_a_long_custom_template_is_still_reachable():
    text = ('Ошибка '+('данные '*1000))+f'<a href="{BASE}">Открыть</a>'
    result = notification_text.brief(text, True)
    assert BASE in bounded(result).links and 'Подробнее в панели' in result


def test_commands_and_raw_logs_are_removed_from_notifications_only():
    text = (f'🔴 <a href="{BASE}">node-a</a>\nУпал Job\n'
            '<pre>private traceback\n'+('trace\n'*1000)+'</pre>\n'
            'Что сделать: <code>ansible-playbook playbooks/fix.yml</code>\nПричина: код 1')
    result = notification_text.brief(text, True)
    assert 'Упал Job' in bounded(result).text and 'Причина: код 1' in result
    assert 'trace' not in result and 'ansible' not in result


def test_last_button_status_and_route_fallback_reason_are_preserved():
    text = f'<a href="{BASE}">node-a</a>\nДиск заполнен\n'+('Факт\n'*20)+'✅ починили'
    result = notification_text.brief(text, True, keep_last=True)
    assert 'Диск заполнен' in bounded(result).text and result.endswith('✅ починили')
    result = notification_text.brief(text+'\n↪ не дошло в чат ops', True)
    assert '↪ не дошло в чат ops' in bounded(result).text


def test_plain_text_does_not_parse_angle_brackets_as_html():
    text = 'Диск < 1 ГБ\n'+('x'*1000)
    result = notification_text.brief(text)
    assert result.startswith('Диск < 1 ГБ') and len(result) < 480


def test_command_detection_does_not_hide_the_failure_description():
    text = '🔴 node-a\nDocker compose не запускается\nПричина: код 1'
    assert notification_text.brief(text) == text
    text = '🔴 node-a\nНе запускается docker compose up\nПричина: код 1'
    assert notification_text.brief(text) == text


def test_malformed_legacy_html_does_not_prevent_delivery():
    text = '🔴 Ошибка\n<![broken]>'+('x'*2000)
    result = notification_text.brief(text, True)
    assert 'Ошибка' in bounded(result).text


@pytest.mark.parametrize('kind', sorted(settings_store.SERVER_ALERT_KINDS))
def test_every_registered_server_kind_keeps_severity_scope_and_full_detail(kind):
    detail = 'Сбой: '+('подробность '*1000)
    server = Server(id=1, name='node-a', token_hash='fixture')
    short = srv_short.short(kind, {}, server, detail, 3)
    original = collector._server_alert_text(kind, server.name, detail, BASE, level=3,
                                             group='ops', ref='srv:1', short=short)
    original.mutes = ['only:this-item']
    result = alerts.short_message(original, 'HTML')
    bounded(result)
    assert result.kind == kind and result.group == 'ops' and result.ref == 'srv:1'
    assert result.mutes == ['only:this-item']
    assert alerts.full_text(result) == alerts.full_text(original)
    assert 'подробность '*50 in alerts.full_text(result)


def test_shortening_keeps_parts_and_shared_sent_receipts():
    parts = [alerts.Msg('original', kind='disk', target='node-a')]
    original = alerts.Msg('🔴 node-a\n'+('problem '*1000), 'servers', 'ops', 'disk', 'node-a', ref='srv:1')
    original.parts, original.sent_to = parts, []
    result = alerts.short_message(original, 'HTML')
    assert result.parts is parts and result.sent_to is original.sent_to
    assert result.full == str(original)


def test_digest_prioritizes_critical_events_and_counts_remaining_critical_events():
    warnings = [alerts.Msg(f'⚠️ warning-{i}', kind='cpu') for i in range(20)]
    failures = [alerts.Msg(f'🚨 failure-{i}', kind='disk') for i in range(5)]
    text = alerts._digest(warnings+failures)
    assert text.splitlines()[:3] == ['🚨 failure-0', '🚨 failure-1', '🚨 failure-2']
    assert text.splitlines()[-1] == 'и еще 22, критичных: 2'
    bounded(text)


class FakeHTTP:
    def __init__(self):
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return Reply()


class Reply:
    status_code = 200
    def json(self):
        return {'ok': True, 'result': {'message_id': 100}}
    def raise_for_status(self):
        pass


async def test_common_route_personal_webhook_and_history_use_the_same_policy(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    async with factory() as session:
        session.add(User(username='operator', password_hash='unused', role='admin', tg_chat_id='personal', tg_alerts=True))
        await session.commit()
    http = FakeHTTP()
    async def client(self):
        return http
    monkeypatch.setattr(alerts._Link, 'client', client)
    full = 'ДИАГНОСТИКА '+('detail '*300)
    msg = alerts.Msg(f'🔴 <a href="{BASE}">node-a</a>\nДиск заполнен\n'+('fact\n'*20),
                     'servers', 'ops', 'disk', 'node-a', full=full, ref='srv:1')
    cfg = {'telegram_token': 'fixture', 'telegram_chat': 'main', 'webhook': 'https://hook.example.test',
           'tg_buttons': False, 'routes': {'main': 'all', 'chats': [
               {'name': 'ops', 'chat': 'route', 'server_groups': ['ops'], 'site_groups': []}]}}
    try:
        assert await alerts.dispatch(cfg, True, [msg], 6, 'HTML', factory)
        chats = {body.get('chat_id') for _, body in http.calls if 'chat_id' in body}
        assert chats == {'main', 'route', 'personal'}
        for _, body in http.calls:
            bounded(body['text'])
            assert 'ДИАГНОСТИКА' not in body['text']
        async with factory() as session:
            event = await session.scalar(select(AlertEvent).where(AlertEvent.kind == 'disk'))
            assert event is not None and event.text == full
    finally:
        await engine.dispose()


async def test_direct_legacy_notifications_and_edits_are_also_bounded(monkeypatch):
    http = FakeHTTP()
    async def client(self):
        return http
    monkeypatch.setattr(alerts._Link, 'client', client)
    text = f'<a href="{BASE}">node-a</a>\nОшибка\n'+('details\n'*30)
    assert not await alerts.send_alert({'telegram_token': 'fixture', 'telegram_chat': 'main',
                                        'webhook': 'https://hook.example.test'}, text, 'HTML')
    await alerts.tg_edit({'telegram_token': 'fixture'}, 'main', 100, text+'✅ снова в норме', {})
    for _, body in http.calls:
        bounded(body['text'])
    assert '✅ снова в норме' in http.calls[-1][1]['text']
