"""A release may reuse only complete, recent CI evidence for its exact commit."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_ci', ROOT / 'ops/release-ci.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = 'a' * 40
REPO = 'example/project'
NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


class API:
    def __init__(self):
        self.workflow = {'id': 10, 'path': gate.WORKFLOW, 'state': 'active'}
        self.run = {'id': 100, 'run_attempt': 1, 'head_sha': SHA, 'workflow_id': 10,
                    'path': gate.WORKFLOW, 'event': 'push', 'head_branch': 'main',
                    'status': 'completed', 'conclusion': 'success',
                    'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO}}
        self.runs = [self.run]
        self.jobs = [dict(id=i, run_id=100, head_sha=SHA, run_attempt=1, name=name,
                          status='completed', conclusion='success', completed_at=NOW.isoformat(),
                          steps=[dict(name=s, status='completed', conclusion='success') for s in steps])
                     for i, (name, steps) in enumerate(gate.REQUIRED.items(), 1)]
        self.changed = False

    def get(self, path):
        if path.endswith('/workflows/ci.yml'):
            return self.workflow
        assert path.endswith('/runs/100')
        return {**self.run, 'run_attempt': 2} if self.changed else self.run

    def pages(self, path, key, **params):
        if key == 'workflow_runs':
            assert params == {'branch': 'main', 'event': 'push', 'head_sha': SHA}
            assert path.endswith('/workflows/ci.yml/runs')
            return self.runs
        assert path.endswith('/runs/100/jobs') and params == {'filter': 'all'}
        return self.jobs


def test_exact_commit_reuses_all_eight_successful_main_jobs():
    receipt = gate.verify(API(), REPO, SHA, NOW)
    assert receipt['sha'] == SHA and receipt['run_id'] == 100 and receipt['jobs'] == 8


@pytest.mark.parametrize('backend', ['backend (3.12)', 'backend (3.14)'])
@pytest.mark.parametrize('state', ['missing', 'skipped'])
def test_host_source_security_checks_cannot_be_omitted(backend, state):
    api = API()
    job = next(job for job in api.jobs if job['name'] == backend)
    name = 'Agent source host security regression'
    step = next(step for step in job['steps'] if step['name'] == name)
    if state == 'missing':
        job['steps'].remove(step)
    else:
        step['conclusion'] = 'skipped'
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


@pytest.mark.parametrize('field,value', [
    ('head_sha', 'b' * 40), ('head_branch', 'topic'), ('event', 'pull_request'),
    ('workflow_id', 20), ('path', '.github/workflows/unrelated.yml'),
    ('status', 'in_progress'), ('conclusion', 'failure'), ('conclusion', 'cancelled'),
    ('repository', {'full_name': 'other/project'}), ('head_repository', {'full_name': 'fork/project'}),
    ('run_attempt', True),
])
def test_untrusted_or_unsuccessful_run_blocks_release(field, value):
    api = API()
    api.run[field] = value
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


@pytest.mark.parametrize('field,value', [('state', 'disabled_manually'), ('path', 'other.yml'), ('id', None)])
def test_wrong_or_disabled_workflow_cannot_supply_evidence(field, value):
    api = API()
    api.workflow[field] = value
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


def test_newer_failed_run_cannot_fall_back_to_older_success():
    api = API()
    api.runs += [{**api.run, 'id': 101, 'conclusion': 'failure'}]
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


@pytest.mark.parametrize('change', ['missing', 'skipped', 'failed', 'wrong_sha', 'stale', 'future',
                                    'bad_time', 'skipped_step', 'missing_step', 'duplicate', 'new_job'])
def test_incomplete_stale_or_ambiguous_job_evidence_blocks_release(change):
    api = API()
    job = api.jobs[0]
    if change == 'missing':
        api.jobs.pop()
    elif change in {'skipped', 'failed'}:
        job['conclusion'] = change
    elif change == 'wrong_sha':
        job['head_sha'] = 'b' * 40
    elif change in {'stale', 'future'}:
        job['completed_at'] = (NOW + timedelta(hours=-25 if change == 'stale' else 1)).isoformat()
    elif change == 'bad_time':
        job['completed_at'] = 'not-a-date'
    elif change == 'skipped_step':
        job['steps'][0]['conclusion'] = 'skipped'
    elif change == 'missing_step':
        job['steps'] = []
    elif change == 'duplicate':
        api.jobs.append({**deepcopy(job), 'id': 99})
    elif change == 'new_job':
        api.jobs.append({**deepcopy(job), 'id': 99, 'name': 'unregistered'})
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


def test_only_failed_job_can_be_rerun_without_repeating_good_jobs():
    api = API()
    api.run['run_attempt'] = 2
    old = api.jobs[0]
    new = {**deepcopy(old), 'id': 99, 'run_attempt': 2}
    old['conclusion'] = 'failure'
    api.jobs.append(new)
    assert gate.verify(api, REPO, SHA, NOW)['attempt'] == 2
    new['conclusion'] = 'failure'
    old['conclusion'] = 'success'
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


def test_rerun_started_during_verification_blocks_release():
    api = API()
    api.changed = True
    with pytest.raises(gate.Rejected):
        gate.verify(api, REPO, SHA, NOW)


def test_paginated_jobs_are_not_silently_truncated():
    class Pages(gate.GitHub):
        def get(self, path, **params):
            return {'total_count': 101, 'jobs': list(range(100)) if params['page'] == 1 else [100]}
    assert len(Pages().pages('fixture', 'jobs')) == 101
    class Missing(Pages):
        def get(self, path, **params):
            return {'total_count': 101, 'jobs': []}
    with pytest.raises(gate.Rejected):
        Missing().pages('fixture', 'jobs')


def test_workflow_keeps_full_ci_and_release_image_scans_without_a_second_suite():
    ci = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    release = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
    assert 'uses: ./.github/workflows/ci.yml' not in release
    assert 'run: python3 ops/release-ci.py' in release and 'actions: read' in release
    assert 'needs: checks' in release
    assert 'name: Scan release backend' in release and 'name: Scan release frontend' in release
    assert release.count('scanners: vuln,secret') == 2
    for steps in gate.REQUIRED.values():
        for step in steps:
            assert 'name: ' + step in ci
    assert 'cancel-in-progress: true' in ci
