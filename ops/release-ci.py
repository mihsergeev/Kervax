#!/usr/bin/env python3
"""Reuse a recent, complete main CI for this exact release commit; fail closed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

WORKFLOW = '.github/workflows/ci.yml'
MAX_AGE = 24 * 3600
REQUIRED = {
    'backend (3.12)': {'Tests', 'Agent source host security regression', 'Backup installer security regression', 'Dependency audit (pip-audit)'},
    'backend (3.14)': {'Tests', 'Agent source host security regression', 'Backup installer security regression', 'Dependency audit (pip-audit)'},
    'migrations': {'Upgrade from scratch', 'Models match the schema', 'Downgrade one and upgrade again', 'Tests on PostgreSQL'},
    'selfcheck': {'Consistency selfcheck'},
    'frontend': {'Install', 'Dependency audit (npm audit)', 'Build'},
    'agent': {'Vet', 'Tests', 'Build'},
    'image-scan (backend, backend/Dockerfile, .)': {'Build image', 'Trivy scan (image)'},
    'image-scan (frontend, frontend/Dockerfile, ./frontend)': {'Build image', 'Trivy scan (image)'},
}


class Rejected(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise Rejected(message)


def positive(value):
    return type(value) is int and value > 0


class GitHub:
    def __init__(self, token=None):
        self.headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10'}
        if token:
            self.headers['Authorization'] = 'Bearer ' + token

    def get(self, path, **params):
        url = 'https://api.github.com/' + path
        if params:
            url += '?' + urlencode(params)
        try:
            with urlopen(Request(url, headers=self.headers), timeout=20) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            require(len(data) <= 4 * 1024 * 1024, 'GitHub response is too large')
            return json.loads(data)
        except HTTPError as exc:
            raise Rejected(f'GitHub API returned HTTP {exc.code}') from None
        except (URLError, TimeoutError, ValueError):
            raise Rejected('GitHub CI evidence is unavailable') from None

    def pages(self, path, key, **params):
        rows = []
        for page in range(1, 11):
            data = self.get(path, per_page=100, page=page, **params)
            total, batch = data.get('total_count'), data.get(key)
            require(type(total) is int and 0 <= total <= 1000 and isinstance(batch, list), 'Incomplete GitHub pagination')
            rows.extend(batch)
            if len(rows) == total:
                return rows
            require(batch and len(rows) < total, 'Incomplete GitHub pagination')
        raise Rejected('GitHub pagination limit exceeded')


def run_identity(run):
    return tuple(run.get(k) for k in ('id', 'run_attempt', 'head_sha', 'workflow_id', 'path', 'event',
                                      'head_branch', 'status', 'conclusion'))


def verify(api, repository, sha, now=None):
    require(isinstance(repository, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'Invalid repository')
    require(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha), 'Expected a full commit SHA')
    now = now or datetime.now(timezone.utc)
    root = 'repos/' + repository + '/actions'
    workflow = api.get(root + '/workflows/ci.yml')
    require(workflow.get('path') == WORKFLOW and workflow.get('state') == 'active'
            and positive(workflow.get('id')), 'The CI workflow is unavailable')
    runs = api.pages(root + '/workflows/ci.yml/runs', 'workflow_runs', branch='main', event='push', head_sha=sha)
    require(runs and all(isinstance(x, dict) and positive(x.get('id')) for x in runs), 'No main CI exists for this commit')
    run = max(runs, key=lambda x: x['id'])
    require(run.get('head_sha') == sha and run.get('head_branch') == 'main' and run.get('event') == 'push'
            and run.get('workflow_id') == workflow['id'] and run.get('path') == WORKFLOW
            and all((run.get(k) or {}).get('full_name', '').lower() == repository.lower()
                    for k in ('repository', 'head_repository')), 'CI belongs to another commit, repository or workflow')
    require(run.get('status') == 'completed' and run.get('conclusion') == 'success'
            and positive(run.get('run_attempt')), 'Latest main CI has not succeeded; finish or rerun it first')
    # Keep earlier successful jobs when only failed jobs were rerun. Never pick
    # an older success over a newer failure, cancellation or skipped execution.
    jobs = api.pages(root + f"/runs/{run['id']}/jobs", 'jobs', filter='all')
    selected, ids = {}, set()
    for job in jobs:
        require(isinstance(job, dict) and positive(job.get('id')) and job['id'] not in ids, 'Invalid CI job evidence')
        ids.add(job['id'])
        attempt, name = job.get('run_attempt'), job.get('name')
        require(positive(attempt) and attempt <= run['run_attempt'] and isinstance(name, str)
                and job.get('run_id') == run['id'] and job.get('head_sha') == sha, 'CI job identity mismatch')
        previous = selected.get(name)
        require(previous is None or previous['run_attempt'] != attempt, 'Ambiguous CI job evidence')
        if previous is None or attempt > previous['run_attempt']:
            selected[name] = job
    require(set(selected) == set(REQUIRED), 'Missing or unregistered CI jobs')
    for name, job in selected.items():
        require(job.get('status') == 'completed' and job.get('conclusion') == 'success', f'CI job is not successful: {name}')
        try:
            timestamp = job['completed_at']
            require(isinstance(timestamp, str), 'CI job has no valid completion time')
            completed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            age = (now - completed).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise Rejected('CI job has no valid completion time') from None
        require(-300 <= age <= MAX_AGE, 'CI is older than 24 hours; rerun CI before releasing')
        steps = job.get('steps') or []
        for needed in REQUIRED[name]:
            found = [s for s in steps if isinstance(s, dict) and s.get('name') == needed]
            require(len(found) == 1 and found[0].get('status') == 'completed'
                    and found[0].get('conclusion') == 'success', f'Required CI step did not pass: {name} / {needed}')
    current = api.get(root + f"/runs/{run['id']}")
    require(run_identity(current) == run_identity(run), 'CI changed while evidence was being verified')
    return {'sha': sha, 'run_id': run['id'], 'attempt': run['run_attempt'], 'jobs': len(selected),
            'url': f"https://github.com/{repository}/actions/runs/{run['id']}", 'checked_at': now.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--sha', default=os.environ.get('GITHUB_SHA'))
    parser.add_argument('--output', default=os.environ.get('GITHUB_OUTPUT'))
    args = parser.parse_args()
    try:
        receipt = verify(GitHub(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')), args.repo, args.sha)
        if args.output:
            with Path(args.output).open('a', encoding='utf-8') as stream:
                stream.write(f"ci_run_id={receipt['run_id']}\nci_attempt={receipt['attempt']}\n")
        print(json.dumps(receipt))
    except Rejected as exc:
        print(f'Release blocked: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
