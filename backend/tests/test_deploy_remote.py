"""Exercise the actual POSIX deployment runner, including failed snapshots and rollback.

Docker is a fault-injecting executable: filesystem changes, tar, flock, process exit
codes and status files are real. No test needs a production panel or credentials.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops/deploy-remote.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX deploy runner")


DOCKER = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
a = sys.argv[1:]
s = pathlib.Path(os.environ['DEPLOY_FIXTURE_STATE'])
with (s/'calls').open('a') as f: f.write(json.dumps(a)+'\n')
mode = os.environ.get('DEPLOY_FIXTURE_MODE', '')
if a[0] == 'inspect':
    if a[2] == '{{.State.StartedAt}}':
        from datetime import datetime, timezone
        print(datetime.fromtimestamp(time.time()-1, timezone.utc).isoformat())
        sys.exit(0)
    if a[-1] == 'kervax-scheduler-1': sys.exit(1)
    service = a[-1].removeprefix('kervax-').removesuffix('-1')
    print('fixture-'+service+':latest' if 'Config.Image' in a[2] else 'sha256:old-'+service)
elif a[:2] == ['image', 'inspect']:
    print('sha256:older-image')
elif a[0] == 'tag':
    if mode == 'tag-fail' and a[-1].endswith(':prev'): sys.exit(23)
elif a[0] == 'compose':
    if 'config' in a:
        print(json.dumps({'services':{'backend':{'environment':{'KERVAX_AGENT_SOURCE_GUARD':'1'}}}}))
    elif 'build' in a:
        (s/'built').touch()
        if mode == 'build-fail': sys.exit(24)
    elif 'up' in a:
        n = int((s/'up-count').read_text())+1 if (s/'up-count').exists() else 1
        (s/'up-count').write_text(str(n))
        (s/'health').write_text('1.8.81' if pathlib.Path('app.txt').read_text() == 'new application' else '1.8.80')
        print('Container fixture-frontend recreated', file=sys.stderr)
        if mode == 'up-fail' or mode == 'first-up-fail' and n == 1: sys.exit(25)
        if mode in ('changed-schema', 'rollback-db-fail', 'rollback-db-empty'):
            (s/'health').write_text('broken')
elif a[0] == 'exec':
    if 'agent_source_guard' in a[-1]:
        print('0' if mode == 'agent-runtime-off' else '1')
    elif 'SELECT id,last_seen FROM servers' in a[-1]:
        if mode == 'agent-baseline-empty': print('[]')
        else:
            baseline=s/'agent-baseline-time'
            if not baseline.exists(): baseline.write_text(str(time.time()-5))
            seen=float(baseline.read_text()) if mode == 'agent-stalled' or not (s/'up-count').exists() else time.time()
            print(json.dumps([[101,seen]]))
    elif 'psql' in a:
        if mode == 'snapshot-db-fail' or mode == 'rollback-db-fail' and (s/'built').exists(): sys.exit(26)
        if mode == 'snapshot-db-empty' or mode == 'rollback-db-empty' and (s/'built').exists(): sys.exit(0)
        if mode == 'snapshot-db-multiple': print('0141\n0142')
        else: print('0142' if mode == 'changed-schema' and (s/'built').exists() else '0141')
    else:
        version = (s/'health').read_text() if (s/'health').exists() else '1.8.80'
        print(json.dumps({'version':version, 'db':'ok'}, separators=(',', ':')))
elif a[0] == 'logs':
    if mode == 'snapshot-logs-fail': sys.exit(27)
    print('fixture service running')
'''


TAR = r'''#!/usr/bin/env python3
import os, pathlib, sys
a = sys.argv[1:]
mode = os.environ.get('DEPLOY_FIXTURE_MODE', '')
if mode == 'snapshot-tar-fail' and a[0] == 'cf' and a[1].endswith('/prev-src.tar'):
    pathlib.Path(a[1]).write_bytes(b'partial archive')
    sys.exit(28)
if mode == 'unpack-fail' and a[0] == 'xf' and a[1].endswith('/src.tar'):
    pathlib.Path(a[-1], 'app.txt').write_text('partially unpacked')
    sys.exit(28)
if mode == 'agent-unpack-fail' and a[0] == 'xf' and a[1].endswith('/agent.tar'):
    pathlib.Path(a[-1], 'manifest.json').write_text('partially unpacked manifest')
    sys.exit(28)
os.execv(os.environ['DEPLOY_FIXTURE_TAR'], [os.environ['DEPLOY_FIXTURE_TAR']]+a)
'''


CP = r'''#!/usr/bin/env python3
import os, pathlib, sys
a = sys.argv[1:]
if os.environ.get('DEPLOY_FIXTURE_MODE') == 'manifest-copy-fail' and a[0].endswith('/manifest.txt'):
    pathlib.Path(a[-1]).write_text('partial manifest')
    sys.exit(28)
os.execv(os.environ['DEPLOY_FIXTURE_CP'], [os.environ['DEPLOY_FIXTURE_CP']]+a)
'''


@pytest.fixture
def deployment(tmp_path):
    panel = tmp_path / 'panel'
    run = panel / '.deploy' / 'fixture-run'
    run.mkdir(parents=True)
    shutil.copyfile(SCRIPT, run / 'run.sh')
    (run / 'params').write_text("FILES='-f compose.yml'\nSERVICES='backend frontend'\n"
                              "EPOCH='fixture'\nVERSION='1.8.81'\nPREV_VERSION='1.8.80'\nREV='fixture'\n")
    old = {'app.txt': 'old application', 'removed.txt': 'old removed source',
           'orphan.txt': 'old orphan source', 'compose.yml': 'old compose'}
    protected = {'.env': 'KERVAX_DB_PASSWORD=fixture-only-placeholder\n',
                 'data/settings.json': 'saved settings', 'agent-signing/local.key': 'fixture marker',
                 'agent-dist/manifest.json': 'old signed manifest', 'agent-dist/manifest.sig': 'old signature'}
    for name, value in {**old, **protected}.items():
        p = panel / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
    (panel / '.kervax-deployed').write_text('app.txt\nremoved.txt\ncompose.yml\n')
    payload = tmp_path / 'new'
    payload.mkdir()
    for name, value in {'app.txt': 'new application', 'new-only.txt': 'new file',
                        'compose.yml': 'new compose'}.items():
        (payload / name).write_text(value)
    with tarfile.open(run / 'src.tar', 'w') as archive:
        for p in payload.iterdir(): archive.add(p, arcname=p.name)
    (run / 'manifest.txt').write_text('app.txt\nnew-only.txt\ncompose.yml\n')
    (run / 'stale.txt').write_text('removed.txt\norphan.txt\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in {'docker': DOCKER, 'tar': TAR, 'cp': CP, 'sleep': '#!/bin/sh\nexit 0\n'}.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o755)
    state = tmp_path / 'state'
    state.mkdir()
    env = {**os.environ, 'PATH': str(bin_dir)+os.pathsep+os.environ['PATH'],
           'DEPLOY_FIXTURE_STATE': str(state), 'DEPLOY_FIXTURE_TAR': shutil.which('tar'),
           'DEPLOY_FIXTURE_CP': shutil.which('cp')}
    return panel, run, state, env, old, protected


def execute(deployment, mode='', kind='run'):
    panel, run, state, env, *_ = deployment
    result = subprocess.run(['sh', str(run / 'run.sh'), kind],
                            env={**env, 'DEPLOY_FIXTURE_MODE': mode},
                            text=True, capture_output=True, timeout=20)
    status = (run / f'{kind}.status').read_text().strip()
    calls = [json.loads(line) for line in (state / 'calls').read_text().splitlines()]
    return result, status, calls


def assert_protected(deployment):
    panel, _, _, _, _, protected = deployment
    for name, content in protected.items(): assert (panel / name).read_text() == content


def assert_old_sources(deployment):
    panel, _, _, _, old, _ = deployment
    for name, content in old.items(): assert (panel / name).read_text() == content
    assert not (panel / 'new-only.txt').exists()
    assert_protected(deployment)


@pytest.mark.parametrize('mode', ['snapshot-db-fail', 'snapshot-db-empty', 'snapshot-db-multiple',
                                 'snapshot-tar-fail', 'snapshot-logs-fail', 'tag-fail'])
def test_failed_snapshot_never_unpacks_or_recreates_containers(deployment, mode):
    result, status, calls = execute(deployment, mode)
    assert result.returncode != 0, result.stdout+result.stderr
    assert status == 'snapshot-failed'
    assert not any(a[0] == 'compose' for a in calls)
    assert_old_sources(deployment)


@pytest.mark.parametrize('managed', [False, True])
def test_failed_build_restores_files_and_immutable_image_tags(deployment, managed):
    panel, run, _, _, _, _ = deployment
    if not managed: (panel / '.kervax-deployed').unlink()
    result, status, calls = execute(deployment, 'build-fail')
    assert result.returncode != 0
    assert status == 'build-failed'
    assert not any(a[0] == 'compose' and 'up' in a for a in calls)
    assert ['tag', 'sha256:old-backend', 'fixture-backend:latest'] in calls
    assert ['tag', 'sha256:old-frontend', 'fixture-frontend:latest'] in calls
    assert (panel / '.kervax-deployed').exists() == managed
    assert_old_sources(deployment)


@pytest.mark.parametrize('mode', ['unpack-fail', 'agent-unpack-fail', 'manifest-copy-fail'])
def test_partially_published_delivery_restores_sources_and_signed_manifest(deployment, mode):
    if mode == 'agent-unpack-fail':
        with tarfile.open(deployment[1] / 'agent.tar', 'w'): pass
    result, status, calls = execute(deployment, mode)
    assert result.returncode != 0 and status == 'failed'
    assert not any(a[0] == 'compose' for a in calls)
    assert_old_sources(deployment)


def test_failed_compose_up_is_not_hidden_by_healthy_version(deployment):
    result, status, calls = execute(deployment, 'first-up-fail')
    assert result.returncode != 0 and status == 'rolled-back', result.stdout+result.stderr
    assert sum(a[0] == 'compose' and 'up' in a for a in calls) == 2
    assert 'compose failed to start the release' in result.stdout
    assert_old_sources(deployment)


def test_failed_compose_rollback_is_not_reported_as_restored(deployment):
    result, status, calls = execute(deployment, 'up-fail')
    assert result.returncode != 0 and status == 'failed'
    assert 'compose could not recreate the previous release' in result.stdout
    assert 'the panel is back on' not in result.stdout
    assert_protected(deployment)


def test_manual_rollback_without_recorded_schema_is_refused(deployment):
    result, status, calls = execute(deployment, kind='rollback')
    assert result.returncode != 0 and status == 'failed'
    assert 'no known previous migration' in result.stdout
    assert not any(a[0] in ('tag', 'compose') for a in calls)
    assert_old_sources(deployment)


@pytest.mark.parametrize('mode', ['changed-schema', 'rollback-db-fail', 'rollback-db-empty'])
def test_unknown_or_changed_schema_prevents_container_rollback(deployment, mode):
    result, status, calls = execute(deployment, mode)
    assert result.returncode != 0 and status == 'failed'
    assert sum(a[0] == 'compose' and 'up' in a for a in calls) == 1
    assert not any(a[0] == 'tag' and a[-1].endswith(':latest') for a in calls)
    assert_protected(deployment)


def test_success_and_repeat_keep_private_settings_and_report_stderr_progress(deployment):
    panel, run, _, _, _, _ = deployment
    result, status, calls = execute(deployment)
    assert result.returncode == 0 and status == 'ok', result.stdout+result.stderr
    assert 'Container fixture-frontend recreated' in result.stdout
    assert (panel / 'app.txt').read_text() == 'new application'
    assert not (panel / 'removed.txt').exists() and not (panel / 'orphan.txt').exists()
    assert_protected(deployment)
    # A repeat has the same source payload and must leave local configuration alone.
    second = panel / '.deploy' / 'fixture-repeat'
    shutil.copytree(run, second)
    (second / 'params').write_text((second / 'params').read_text().replace("PREV_VERSION='1.8.80'", "PREV_VERSION='1.8.81'"))
    repeat = subprocess.run(['sh', str(second / 'run.sh'), 'run'],
                            env=deployment[3], text=True, capture_output=True, timeout=20)
    assert repeat.returncode == 0, repeat.stdout+repeat.stderr
    assert (second / 'run.status').read_text().strip() == 'ok'
    assert_protected(deployment)


def enable_guard_fixture(deployment):
    panel, run, state, env, _, protected = deployment
    protected['.env'] += 'KERVAX_AGENT_SOURCE_GUARD=1\n'
    (panel / '.env').write_text(protected['.env'])
    payload = state / 'guard-payload'
    payload.mkdir()
    helper = Path(__file__).resolve().parents[2] / 'ops/agent-source-live-check.py'
    (payload / 'agent-source-live-check.py').write_text(helper.read_text().replace('def verify(path, since, timeout=60):', 'def verify(path, since, timeout=0):').replace('if os.geteuid() != 0:', 'if False:  # fixture owns its private baseline directory'))
    shutil.copyfile(helper.with_name('agent-source-guard-enabled.py'), payload / 'agent-source-guard-enabled.py')
    (payload / 'agent-proxy-peers-setup.sh').write_text('#!/bin/sh\necho \'["peer-refresh"]\' >> "$DEPLOY_FIXTURE_STATE/calls"\n')
    with tarfile.open(run / 'src.tar', 'a') as archive:
        for source in payload.iterdir(): archive.add(source, arcname='ops/' + source.name)
    with (run / 'manifest.txt').open('a') as manifest:
        for source in payload.iterdir(): manifest.write('ops/' + source.name + '\n')
    systemctl = Path(env['PATH'].split(os.pathsep)[0]) / 'systemctl'
    systemctl.write_text('#!/bin/sh\necho \'["host-peer-refresh"]\' >> "$DEPLOY_FIXTURE_STATE/calls"\n')
    systemctl.chmod(0o755)


@pytest.mark.parametrize('mode,expected,up_count', [('agent-baseline-empty', 'snapshot-failed', 0), ('agent-stalled', 'rolled-back', 2), ('agent-runtime-off', 'rolled-back', 2), ('', 'ok', 1)])
def test_guard_requires_prefresh_agents_to_report_after_new_backend_start(deployment, mode, expected, up_count):
    enable_guard_fixture(deployment)
    result, status, calls = execute(deployment, mode)
    assert status == expected, result.stdout + result.stderr
    assert (result.returncode == 0) == (expected == 'ok')
    assert sum(a[0] == 'compose' and 'up' in a for a in calls) == up_count
    if expected != 'ok':
        assert_old_sources(deployment)
    if mode == 'agent-baseline-empty':
        assert sum(a[0] == 'exec' and 'SELECT id,last_seen FROM servers' in a[-1] for a in calls) == 1
    if mode == 'agent-stalled':
        assert 'reports not advanced for IDs: 101' in result.stdout
        assert sum(a[0] == 'exec' and 'SELECT id,last_seen FROM servers' in a[-1] for a in calls) == 2
    if expected == 'ok':
        assert '1 agents reported after backend start' in result.stdout


def test_manual_guarded_rollback_refreshes_restored_container_peers(deployment):
    enable_guard_fixture(deployment)
    result, status, _ = execute(deployment)
    assert result.returncode == 0 and status == 'ok', result.stdout + result.stderr
    result, status, calls = execute(deployment, kind='rollback')
    assert result.returncode == 0 and status == 'rolled-back', result.stdout + result.stderr
    assert sum(call[0] == 'host-peer-refresh' for call in calls) == 2
    assert_old_sources(deployment)
