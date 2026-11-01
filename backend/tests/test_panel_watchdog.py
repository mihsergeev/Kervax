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
