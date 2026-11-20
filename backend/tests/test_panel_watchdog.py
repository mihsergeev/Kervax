"""The independent host watchdog also stays concise and never dumps stage diagnostics."""
from pathlib import Path
import os
import shutil
import subprocess
import time

import pytest


@pytest.mark.parametrize('problem', ['stage', 'tick', 'channel', 'stale'])
def test_watchdog_short_alarm_persistent_debounce_and_recovery(tmp_path, problem):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('Bash is required')
    bindir = tmp_path/'bin'
    bindir.mkdir()
    capture, state, heartbeat = (tmp_path/name for name in ('sent.txt', 'state', 'heartbeat'))
    for name, body in {
        'curl': '#!/bin/sh\nfor arg in "$@"; do case "$arg" in text=*) printf "%s\\n" "${arg#text=}" >> "$CAPTURE";; esac; done\n',
        'hostname': '#!/bin/sh\nprintf "panel-fixture\\n"\n',
    }.items():
        path = bindir/name
        path.write_text(body, encoding='utf-8', newline='\n')
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir)+os.pathsep+os.environ['PATH'],
           'CAPTURE': capture.as_posix(), 'KERVAX_HEARTBEAT': heartbeat.as_posix(),
           'KERVAX_WD_STATE': state.as_posix(), 'KERVAX_WD_STRIKES': '1'}
    now = int(time.time())
    values = {'ts': now, 'alerts_ok': 1, 'tick': now, 'tick_max': 900,
              'stage_fail': '0', 'panel': 'x'*2000, 'tg_token': 'fixture', 'tg_chat': 'fixture'}
    if problem == 'stage': values['stage_fail'] = '20 '+('long diagnostic '*1000)
    if problem == 'tick': values['tick'] = now-2000
    if problem == 'channel': values['alerts_ok'] = 0
    if problem == 'stale': values['ts'] = now-2000
    def write():
        heartbeat.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf-8')
    def run():
        p = subprocess.run([bash, str(Path(__file__).parents[2]/'ops/panel-watchdog.sh')],
                           env=env, capture_output=True, timeout=20)
        assert p.returncode == 0, p.stderr.decode(errors='replace')
    write()
    run()
    alarm = capture.read_text(encoding='utf-8').splitlines()
    assert len(alarm) == 1 and len(alarm[0]) < 150
    assert 'panel-fixture' in alarm[0] and 'long diagnostic' not in alarm[0]
    run()
    assert capture.read_text(encoding='utf-8').splitlines() == alarm
    values.update(ts=now, tick=now, alerts_ok=1, stage_fail='0')
    write()
    run()
    assert len(capture.read_text(encoding='utf-8').splitlines()) == 2
    assert 'back to normal' in capture.read_text(encoding='utf-8')


def _watchdog_curl_calls(tmp_path, values, extra='', link=False):
    """Run the watchdog against a failing heartbeat; return the argv of every curl call.
    link: the heartbeat path is a symlink to the written file."""
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('Bash is required')
    bindir = tmp_path/'bin'
    bindir.mkdir()
    args, state, heartbeat = (tmp_path/name for name in ('args.txt', 'state', 'heartbeat'))
    curl = bindir/'curl'
    curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$ARGS"\nprintf "==\\n" >> "$ARGS"\n',
                    encoding='utf-8', newline='\n')
    curl.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir)+os.pathsep+os.environ['PATH'], 'ARGS': args.as_posix(),
           'KERVAX_HEARTBEAT': heartbeat.as_posix(), 'KERVAX_WD_STATE': state.as_posix(),
           'KERVAX_WD_STRIKES': '1'}
    values = {'ts': int(time.time()), 'alerts_ok': 0, 'panel': 'panel.example.test', **values}
    target = tmp_path/'heartbeat.real' if link else heartbeat
    target.write_text(''.join(f'{k}={v}\n' for k, v in values.items())+extra, encoding='utf-8')
    if link:
        heartbeat.symlink_to(target)
    p = subprocess.run([bash, str(Path(__file__).parents[2]/'ops/panel-watchdog.sh')],
                       env=env, capture_output=True, timeout=20)
    assert p.returncode == 0, p.stderr.decode(errors='replace')
    text = args.read_text(encoding='utf-8') if args.exists() else ''
    return [call.splitlines() for call in text.split('==\n') if call]


def test_watchdog_heartbeat_values_never_become_curl_options(tmp_path):
    """The heartbeat is written by the container, curl runs as root: "-K <heartbeat>" in webhook
    plus injected url=/output= lines used to make curl read the heartbeat as its config."""
    hb = (tmp_path/'heartbeat').as_posix()
    calls = _watchdog_curl_calls(tmp_path, {
        'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890',
        'tg_api': 'file:///etc/passwd', 'webhook': f'-K{hb}'},
        extra=f'url=file:///etc/passwd\noutput={(tmp_path/"pwned").as_posix()}\n')
    assert not [a for call in calls for a in call if a.startswith('-K') or hb in a]
    assert len(calls) == 1, calls
    tg = calls[0]
    # a broken mirror address falls back to the default API; a negative chat id still works
    assert tg[tg.index('--url')+1] == 'https://api.telegram.org/bot123456:TEST-token/sendMessage'
    assert 'chat_id=-1001234567890' in tg
    assert tg[tg.index('--proto')+1] == '=http,https'


def test_watchdog_sends_to_https_webhook_and_mirror_via_url(tmp_path):
    calls = _watchdog_curl_calls(tmp_path, {
        'tg_token': '123456:TEST-token', 'tg_chat': '@alerts_example', 'tg_api': 'http://[2001:db8::1]:8081',
        'webhook': 'https://hook.example.test/kervax'})
    urls = sorted(call[call.index('--url')+1] for call in calls)
    assert urls == ['http://[2001:db8::1]:8081/bot123456:TEST-token/sendMessage', 'https://hook.example.test/kervax']
    assert all(call[call.index('--proto')+1] == '=http,https' for call in calls)
    # -q first: root's ~/.curlrc is not read; -g: no [1-99999] globbing in the address
    assert all(call[0] == '-q' and '-g' in call for call in calls)


def _alarm(calls):
    return [a[len('text='):] for call in calls for a in call if a.startswith('text=')]


def test_watchdog_heartbeat_numbers_never_run_code(tmp_path):
    """ts went into $(( )) as is: bash expands an array index there, so NOW[$(cmd)0] ran cmd as root.
    A broken ts is a broken heartbeat: the alarm still comes."""
    marker = tmp_path/'marker'
    calls = _watchdog_curl_calls(tmp_path, {
        'ts': f'NOW[$(touch {marker.as_posix()})0]', 'alerts_ok': 1,
        'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890'})
    assert not marker.exists()
    assert len(_alarm(calls)) == 1 and 'no heartbeat' in _alarm(calls)[0]


def test_watchdog_leading_zero_tick_still_alarms(tmp_path):
    """tick=08 is not octal: "value too great for base" used to cut the checks and mute the alarm."""
    calls = _watchdog_curl_calls(tmp_path, {
        'alerts_ok': 1, 'tick': '08', 'tick_max': 900, 'stage_fail': '0',
        'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890'})
    assert len(_alarm(calls)) == 1 and 'tick stuck' in _alarm(calls)[0]


def test_watchdog_symlinked_heartbeat_counts_as_missing(tmp_path):
    """A symlink is not read at all: the watchdog records the problem, and without credentials from
    the heartbeat it has nobody to send it to (as with a missing heartbeat)."""
    calls = _watchdog_curl_calls(tmp_path, {
        'alerts_ok': 1, 'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890'}, link=True)
    assert calls == []
    assert (tmp_path/'state').read_text(encoding='utf-8').splitlines() == ['problem', '1']


@pytest.mark.parametrize('token,chat', [('-o/tmp/x', '-1001234567890'), ('123456:TEST-token', '-100 123'),
                                        ('123456:TEST-token', '-K/tmp/x'), ('123456:TEST-token', '-')])
def test_watchdog_skips_unsafe_telegram_values(tmp_path, token, chat):
    calls = _watchdog_curl_calls(tmp_path, {'tg_token': token, 'tg_chat': chat})
    assert calls == []


def test_watchdog_heartbeat_from_the_future_alarms(tmp_path):
    """A negative age used to pass as a fresh heartbeat for as long as ts stayed ahead."""
    calls = _watchdog_curl_calls(tmp_path, {
        'ts': int(time.time()) + 3600, 'alerts_ok': 1,
        'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890'})
    assert len(_alarm(calls)) == 1 and 'in the future' in _alarm(calls)[0]


def test_watchdog_drops_webhook_with_control_characters(tmp_path):
    """A heartbeat written past the API (old value, restored backup): a webhook ending in \\r is not
    used, Telegram still gets the alarm."""
    calls = _watchdog_curl_calls(tmp_path, {
        'tg_token': '123456:TEST-token', 'tg_chat': '-1001234567890',
        'webhook': 'https://hook.example.test/kervax\r'})
    assert len(calls) == 1, calls
    assert calls[0][calls[0].index('--url')+1] == 'https://api.telegram.org/bot123456:TEST-token/sendMessage'
