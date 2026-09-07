"""Deployment script contract tests with a fake provider CLI; no resources created."""
import os
import subprocess
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'api/scripts/deploy.sh'


def test_offline_plan_needs_no_credentials_or_flyctl():
    result = subprocess.run(['bash', str(SCRIPT), '--app-name', 'handoff-plan', '--dry-run'],
                            env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Offline plan' in result.stdout


@pytest.mark.parametrize('failure', ['', 'lookup', 'health', 'missing-admin', 'missing-webhook'])
def test_existing_deploy_preserves_secrets_and_fails_closed(tmp_path, failure):
    repo = tmp_path / 'repo'
    (repo / 'api/scripts').mkdir(parents=True)
    shutil.copy(SCRIPT, repo / 'api/scripts/deploy.sh')
    shutil.copy(ROOT / 'api/fly.toml', repo / 'api/fly.toml')
    calls = tmp_path / 'calls'
    fly = tmp_path / 'fly'
    fly.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as f: f.write(' '.join(args) + '\\n')
if args == ['version']: print('test fly')
elif args == ['auth', 'whoami']: print('test@example.com')
elif args[:2] == ['apps', 'list']: print(json.dumps([{'Name': 'handoff-existing'}]))
elif args[:2] == ['ips', 'list']: print(json.dumps([{'Type': 'v4'}, {'Type': 'v6'}]))
elif args[:2] == ['secrets', 'list']:
    if os.environ.get('FAILURE') == 'lookup': sys.exit(1)
    omitted = {'missing-admin': 'ADMIN_TOKEN', 'missing-webhook': 'WEBHOOK_SECRET'}.get(os.environ.get('FAILURE'))
    print(json.dumps([{'name': n} for n in ('DATABASE_URL', 'AWS_ACCESS_KEY_ID', 'ADMIN_TOKEN', 'WEBHOOK_SECRET') if n != omitted]))
elif args[:2] == ['volumes', 'list']: print(json.dumps([{'name': 'courtside_work'}]))
elif args[:2] == ['secrets', 'import']:
    with open(os.environ['CALL_LOG'], 'a') as f:
        f.write('secret names: ' + ','.join(line.split('=')[0] for line in sys.stdin.read().splitlines()) + '\\n')
elif args[0] == 'deploy':
    config = Path(args[args.index('-c')+1]).read_text()
    assert 'app = "handoff-existing"' in config
    assert "soft-water" not in config
else: raise AssertionError('unexpected mutation: ' + str(args))
''')
    fly.chmod(0o755)
    for name, body in [('curl', 'printf "${HTTP_CODE:-200}"'), ('sleep', ':')]:
        p = tmp_path / name
        p.write_text('#!/bin/sh\n' + body + '\n')
        p.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in
           ('DATABASE_URL', 'S3_BUCKET', 'APP_NAME', 'BUCKET_NAME', 'WEBHOOK_ALLOWED_HOSTS', 'CORS_ORIGINS', 'DEFAULT_MODEL', 'OPENROUTER_URL')}
    env.update(PATH=f'{tmp_path}:' + os.environ['PATH'], CALL_LOG=str(calls), FAILURE=failure,
               HTTP_CODE='503' if failure == 'health' else '200',
               FLY_API_TOKEN='test-fly-private', OPENROUTER_API_KEY='test-model-private')
    result = subprocess.run(['bash', str(repo / 'api/scripts/deploy.sh'), '--app-name', 'handoff-existing',
                             '--org', 'test', '--yes'], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) == (failure not in ('lookup', 'health')), result.stdout + result.stderr
    assert 'test-model-private' not in result.stdout + result.stderr + calls.read_text()
    if failure != 'lookup':
        assert 'secret names: OPENROUTER_API_KEY' in calls.read_text()
    else:
        assert 'secrets import' not in calls.read_text()
    assert not list(repo.glob('fly.deploy.*.toml'))
    if failure.startswith('missing-'):
        credentials = repo / 'fly-handoff-existing-credentials.txt'
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert ('ADMIN_TOKEN=' if failure == 'missing-admin' else 'WEBHOOK_SECRET=') in credentials.read_text()
