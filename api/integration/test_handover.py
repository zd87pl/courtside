"""Stateful regressions for handover controls against disposable Postgres/S3."""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

if os.environ.get('COURTSIDE_INTEGRATION') != '1':
    pytest.skip('requires disposable services', allow_module_level=True)

from .test_stack import client, account
from courtside_api import billing, checkpoints, db, jobs, privacy, storage, webhooks
from courtside_api.config import settings


def headers(account, owner='one', idem=None):
    result = {'Authorization': f"Bearer {account['api_key']}", 'X-Courtside-User-Id': owner}
    if idem:
        result['Idempotency-Key'] = idem
    return result


def reserve(client, account, owner='one', **body):
    response = client.post('/v1/uploads', headers=headers(account, owner),
                           json=dict(filename='clip.mp4', size_bytes=8, **body))
    assert response.status_code == 201, response.text
    return response.json()


def test_user_scope_covers_reads_mutations_and_export(client, account):
    up = reserve(client, account)
    jid = up['job_id']
    other = headers(account, 'other')
    for method, path in [('get', f'/v1/jobs/{jid}'), ('get', f'/v1/jobs/{jid}/export'),
                         ('get', f'/v1/jobs/{jid}/usage'), ('get', f'/v1/jobs/{jid}/report'),
                         ('get', f'/v1/uploads/{jid}/parts'), ('post', f'/v1/uploads/{jid}/refresh'),
                         ('post', f'/v1/jobs/{jid}/cancel'), ('delete', f'/v1/jobs/{jid}')]:
        assert client.request(method, path, headers=other).status_code == 404, path
    assert client.post(f'/v1/jobs/{jid}/start', headers=other, json={}).status_code == 404
    assert client.get('/v1/jobs', headers=other).json()['jobs'] == []
    assert len(client.get('/v1/jobs', headers=headers(account)).json()['jobs']) == 1
    assert client.get('/v1/jobs', headers={'Authorization': f"Bearer {account['api_key']}"}).status_code == 400


def test_concurrent_idempotency_reuses_multipart_plan_and_rejects_conflicts(client, account):
    body = {'filename': 'clip.mp4', 'multipart': True, 'size_bytes': 8}
    h = headers(account, idem='upload-device-123')
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(lambda _: client.post('/v1/uploads', headers=h, json=body), range(5)))
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()['job_id'] for r in responses}) == 1
    assert len({r.json()['multipart_id'] for r in responses}) == 1
    assert client.post('/v1/uploads', headers=h, json=dict(body, size_bytes=9)).status_code == 409
    assert client.post('/v1/uploads', headers=headers(account, 'other', 'upload-device-123'), json=body).status_code == 409
    up = responses[0].json()
    result = httpx.put(up['parts'][0]['url'], content=b'abcdefgh')
    assert result.status_code == 200
    parts = client.get(f"/v1/uploads/{up['job_id']}/parts", headers=h).json()['parts']
    assert parts == [{'part_number': 1, 'etag': result.headers['etag'], 'size_bytes': 8}]
    refreshed = client.post(f"/v1/uploads/{up['job_id']}/refresh", headers=h).json()
    assert refreshed['multipart_id'] == up['multipart_id']
    assert client.post(f"/v1/uploads/{up['job_id']}/complete", headers=h,
                       json={'parts': [{'part_number': 1, 'etag': parts[0]['etag']}]}).status_code == 200


def test_pending_quota_and_hourly_rate_are_account_wide(client, account, monkeypatch):
    base = settings()
    monkeypatch.setattr(jobs, 'settings', lambda: replace(base, max_pending_uploads=2, uploads_per_hour=3))
    first = reserve(client, account)
    reserve(client, account, 'two')
    assert client.post('/v1/uploads', headers=headers(account, 'three'), json={'filename': 'x.mp4'}).status_code == 429
    assert client.post(f"/v1/jobs/{first['job_id']}/cancel", headers=headers(account)).status_code == 200
    third = reserve(client, account)
    assert client.post(f"/v1/jobs/{third['job_id']}/cancel", headers=headers(account)).status_code == 200
    assert client.post('/v1/uploads', headers=headers(account), json={'filename': 'x.mp4'}).status_code == 429


def running(account):
    aid = uuid.UUID(account['account_id'])
    row = jobs.create(aid, filename='x.mp4', content_type='video/mp4', video_key_fn=storage.video_key)
    jobs.enqueue(row['id'], aid, {'model': settings().default_model}, None)
    job = jobs.claim('ledger-worker')
    assert job['id'] == row['id']
    return job


def set_context(monkeypatch, job):
    for key, value in {'COURTSIDE_JOB_ID': job['id'], 'COURTSIDE_JOB_ATTEMPT': job['attempts'],
                       'COURTSIDE_JOB_WORKER': job['worker_id']}.items():
        monkeypatch.setenv(key, str(value))


def test_spend_reservations_settlement_unknown_failures_and_lease_fencing(client, account, monkeypatch):
    job = running(account)
    set_context(monkeypatch, job)
    with db.conn() as c:
        c.execute('UPDATE accounts SET monthly_usd_cap = 1 WHERE id = %s', (account['account_id'],))
    first = billing.begin(settings().default_model)
    second = billing.begin(settings().default_model)
    with pytest.raises(billing.BudgetExceeded): billing.begin(settings().default_model)
    billing.finish(first, response=SimpleNamespace(id='generation-test', usage=SimpleNamespace(
        cost=0.1, prompt_tokens=20, completion_tokens=5)))
    billing.finish(second, error=SimpleNamespace(status_code=500))
    usage = billing.summary(account['account_id'])
    assert usage['confirmed_usd'] == 0.1 and usage['reserved_usd'] == 0.5
    assert usage['unsettled_requests'] == 1
    with pytest.raises(billing.BudgetExceeded): billing.begin('unapproved/model')
    jobs.request_cancel(job['id'], job['account_id'])
    with pytest.raises(billing.BudgetExceeded): billing.begin(settings().default_model)
    assert jobs.month_usage(job['account_id'])[0] == 0.6


def test_checkpoint_restores_only_matching_source_options_and_code(client, account, monkeypatch, tmp_path):
    job = running(account)
    set_context(monkeypatch, job)
    first = tmp_path / 'first'
    first.mkdir()
    checkpoints.restore(job, first, 'etag1')
    out = first / 'out'
    out.mkdir()
    (out / 'clip_000.json').write_text('{"start_s":0}')
    checkpoints.save(out)
    job = jobs.get(job['id'])
    assert job['checkpoint_key']
    second = tmp_path / 'second'
    second.mkdir()
    checkpoints.restore(job, second, 'etag1')
    assert json.loads((second / 'out' / 'clip_000.json').read_text()) == {'start_s': 0}
    third = tmp_path / 'third'
    third.mkdir()
    checkpoints.restore(job, third, 'different-etag')
    assert not (third / 'out').exists()
    old = job['checkpoint_key']
    with db.conn() as c: c.execute('UPDATE jobs SET attempts = attempts + 1 WHERE id = %s', (job['id'],))
    (out / 'clip_001.json').write_text('{}')
    checkpoints.save(out)
    assert jobs.get(job['id'])['checkpoint_key'] == old
    storage.purge_prefix(f"checkpoints/{job['account_id']}/{job['id']}/")


def test_outbox_recovery_and_redelivery_preserve_delivery_id(client, account, monkeypatch):
    job = running(account)
    with db.conn() as c:
        c.execute("UPDATE jobs SET webhook_url = 'https://example.com/callback', status = 'failed', finished_at = now() WHERE id = %s", (job['id'],))
    assert webhooks.enqueue_pending() == 1
    assert webhooks.enqueue_pending() == 0
    calls = []
    class Opener:
        def open(self, req, timeout):
            body = req.data
            assert webhooks.verify(body, req.get_header('X-courtside-signature'), settings().webhook_secret)
            calls.append(json.loads(body))
            if len(calls) == 1: raise TimeoutError()
            return Response()
    class Response:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(webhooks.urllib.request, 'build_opener', lambda *a: Opener())
    assert webhooks.dispatch_one()
    with db.conn() as c:
        row = c.execute('SELECT * FROM webhook_outbox WHERE job_id = %s', (job['id'],)).fetchone()
        assert row['attempts'] == 1 and row['delivered_at'] is None
        c.execute("UPDATE webhook_outbox SET lease_until = now() - interval '1 minute', next_attempt_at = now() - interval '1 minute' WHERE id = %s", (row['id'],))
    assert webhooks.dispatch_one()
    assert calls[0]['delivery_id'] == calls[1]['delivery_id']
    with db.conn() as c:
        assert c.execute('SELECT delivered_at FROM webhook_outbox WHERE id = %s', (row['id'],)).fetchone()['delivered_at']


def test_deletion_hides_data_then_purges_objects_and_audits(client, account):
    up = reserve(client, account)
    jid = up['job_id']
    assert httpx.put(up['upload_url'], content=b'abcdefgh', headers={'Content-Type': 'video/mp4'}).status_code == 200
    prefix = f"reports/{account['account_id']}/{jid}/"
    storage.client().put_object(Bucket=storage.bucket(), Key=prefix+'report.html', Body=b'private')
    response = client.delete(f'/v1/jobs/{jid}', headers=headers(account))
    assert response.status_code == 202 and response.json()['state'] == 'pending'
    assert client.get(f'/v1/jobs/{jid}', headers=headers(account)).status_code == 404
    assert client.get(f'/v1/deletions/{jid}', headers=headers(account, 'other')).status_code == 404
    assert client.delete(f'/v1/jobs/{jid}', headers=headers(account)).status_code == 202
    privacy.sweep()
    assert storage.head(prefix+'report.html') is None
    assert client.get(f'/v1/deletions/{jid}', headers=headers(account)).json()['state'] == 'pending'
    with db.conn() as c:
        c.execute("UPDATE jobs SET expires_at = now() - interval '1 hour' WHERE id = %s", (jid,))
    privacy.sweep()
    assert client.get(f'/v1/deletions/{jid}', headers=headers(account)).json()['state'] == 'deleted'
    with db.conn() as c:
        row = c.execute('SELECT filename, session_json, log_tail FROM jobs WHERE id = %s', (jid,)).fetchone()
        assert all(v is None for v in row.values())
        assert c.execute("SELECT id FROM deletion_audit WHERE job_id = %s AND action = 'deleted'", (jid,)).fetchone()


def test_operations_are_admin_only(client, account):
    assert client.get('/v1/admin/operations', headers=headers(account)).status_code == 403
    response = client.get('/v1/admin/operations', headers={'X-Admin-Token': os.environ['ADMIN_TOKEN']})
    assert response.status_code == 200, response.text
    assert response.json()['migrations'] == ['001', '002', '003']
    assert 'total_slots' in response.json()['workers']
    assert client.get('/v1/admin/usage/unsettled', headers=headers(account)).status_code == 403
    assert client.get('/v1/admin/usage/unsettled', headers={'X-Admin-Token': os.environ['ADMIN_TOKEN']}).status_code == 200


def test_failed_webhook_replay_is_admin_only_and_preserves_delivery_id(client, account):
    up = reserve(client, account)
    with db.conn() as c:
        row = c.execute("""INSERT INTO webhook_outbox(job_id, event, url, payload, failed_at, attempts)
            VALUES (%s, 'job.completed', 'https://example.com/callback', '{}', now(), 5) RETURNING id""",
            (up['job_id'],)).fetchone()
    path = f"/v1/admin/webhooks/{row['id']}/retry"
    assert client.post(path, headers=headers(account)).status_code == 403
    admin = {'X-Admin-Token': os.environ['ADMIN_TOKEN']}
    assert client.post(path, headers=admin).json() == {'delivery_id': str(row['id']), 'status': 'pending'}
    assert client.post(path, headers=admin).status_code == 409


def test_cleanup_rotates_past_failed_or_unexpired_batches(client, account, monkeypatch):
    # Insert directly: this tests maintenance scheduling, not admission quotas.
    with db.conn() as c:
        for i in range(26):
            c.execute("""INSERT INTO jobs(account_id, status, video_key, expires_at, finished_at)
                VALUES (%s, 'cancelled', %s, now() + interval '1 day', now())""",
                (account['account_id'], f'fairness/{i}'))
    visited = set()
    def fail_storage(prefix):
        if prefix.startswith(f"uploads/{account['account_id']}/"):
            visited.add(prefix)
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(storage, 'purge_prefix', fail_storage)
    privacy.sweep()
    assert len(visited) <= 25
    privacy.sweep()
    assert len(visited) == 26


def test_migration_adopts_legacy_schema_preserves_rows_and_checks_checksums(client):
    from contextlib import contextmanager
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    schema = 'upgrade_' + uuid.uuid4().hex
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True, row_factory=dict_row) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
        try:
            for statement in db._statements(db.SCHEMA_PATH.read_text()): c.execute(statement)
            aid = c.execute("INSERT INTO accounts(name) VALUES ('legacy') RETURNING id").fetchone()['id']
            @contextmanager
            def connection(): yield c
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(db, 'conn', connection)
                db.bootstrap()
                db.bootstrap()
                assert c.execute('SELECT name FROM accounts WHERE id = %s', (aid,)).fetchone()['name'] == 'legacy'
                assert c.execute('SELECT owner_id FROM jobs LIMIT 1').fetchone() is None
                assert c.execute('SELECT count(*) AS n FROM schema_migrations').fetchone()['n'] == 3
                c.execute("UPDATE schema_migrations SET checksum = 'changed' WHERE version = '002'")
                with pytest.raises(RuntimeError, match='checksum changed'): db.bootstrap()
        finally:
            c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_purge_removes_all_versions_and_incomplete_uploads(client, monkeypatch):
    name = 'erasure-' + uuid.uuid4().hex
    s3 = storage.client()
    s3.create_bucket(Bucket=name)
    with monkeypatch.context() as mp:
        mp.setattr(storage, 'bucket', lambda: name)
        try:
            s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={'Status': 'Enabled'})
            for content in [b'old', b'new']:
                s3.put_object(Bucket=name, Key='reports/job/report.html', Body=content)
            s3.delete_object(Bucket=name, Key='reports/job/report.html')
            s3.create_multipart_upload(Bucket=name, Key='reports/job/incomplete')
            storage.purge_prefix('reports/job/')
            versions = s3.list_object_versions(Bucket=name)
            assert not versions.get('Versions') and not versions.get('DeleteMarkers')
            assert not s3.list_multipart_uploads(Bucket=name).get('Uploads')
        finally:
            storage.purge_prefix('reports/job/')
            s3.delete_bucket(Bucket=name)


@pytest.mark.parametrize('legacy', [False, True])
def test_backup_restore_into_empty_database_replays_newer_deletions(client, account, tmp_path, legacy):
    import shutil
    import subprocess
    import sys
    import psycopg
    from psycopg import sql
    if not os.environ.get('COURTSIDE_TEST_PG_CONTAINER') and not shutil.which('pg_dump'):
        pytest.skip('requires PostgreSQL client tools or COURTSIDE_TEST_PG_CONTAINER')
    up = reserve(client, account)
    jid = up['job_id']
    script = str(Path(__file__).resolve().parents[1] / 'scripts/database.py')
    dump, manifest = tmp_path / 'backup.dump', tmp_path / 'deletions.json'
    env = dict(os.environ)
    if env.get('COURTSIDE_TEST_PG_CONTAINER'): env['PG_CONTAINER'] = env['COURTSIDE_TEST_PG_CONTAINER']
    def run(*args):
        result = subprocess.run([sys.executable, script, *map(str, args)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    if legacy:
        legacy_name = 'legacy_backup_' + uuid.uuid4().hex
        with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(legacy_name)))
            try:
                env['DATABASE_URL'] = psycopg.conninfo.make_conninfo(os.environ['DATABASE_URL'], dbname=legacy_name)
                with psycopg.connect(env['DATABASE_URL']) as old:
                    for statement in db._statements(db.SCHEMA_PATH.read_text()): old.execute(statement)
                    old.execute("INSERT INTO accounts(id, name) VALUES (%s, 'legacy')", (account['account_id'],))
                    old.execute("INSERT INTO jobs(id, account_id, video_key, filename) VALUES (%s, %s, 'old/source', 'private.mp4')",
                                (jid, account['account_id']))
                run('backup', dump)
            finally:
                env['DATABASE_URL'] = os.environ['DATABASE_URL']
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(legacy_name)))
    else:
        run('backup', dump)
    assert client.delete(f'/v1/jobs/{jid}', headers=headers(account)).status_code == 202
    run('export-deletions', manifest)
    name = 'restore_' + uuid.uuid4().hex
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        try:
            env['RESTORE_DATABASE_URL'] = psycopg.conninfo.make_conninfo(os.environ['DATABASE_URL'], dbname=name)
            run('restore', dump, '--deletions', manifest)
            with psycopg.connect(env['RESTORE_DATABASE_URL']) as restored:
                row = restored.execute('SELECT deletion_requested_at, filename, status FROM jobs WHERE id = %s', (jid,)).fetchone()
                assert row[0] and row[1] is None and row[2] == 'cancelled'
            # A repeat must refuse a populated target rather than overwrite it.
            result = subprocess.run([sys.executable, script, 'restore', str(dump), '--deletions', str(manifest)], env=env, capture_output=True)
            assert result.returncode != 0
        finally:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_worker_pipeline_with_local_provider_stub_records_cost_and_publishes(client, account, monkeypatch, tmp_path):
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from courtside_api import worker
    video = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=320x180:r=10:d=4',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True)
    counter = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"object":"list","data":[]}')
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            counter.append(req)
            content = json.dumps({'start_s': 0, 'end_s': 4, 'rally_summary': 'Synthetic fixture',
                                  'confidence': 'low', 'strokes': [], 'notes': ''}) if 'response_format' in req else '# Test report\nSynthetic test only.'
            data = {'id': 'stub-' + str(len(counter)), 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70, 'cost': 0.001}}
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps(data).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}/v1'
        base = settings()
        monkeypatch.setattr(worker, 'settings', lambda: replace(base, openrouter_url=url,
                            work_dir=str(tmp_path/'work'), heartbeat_s=0.1, job_timeout_s=60))
        monkeypatch.setenv('OPENROUTER_URL', url)
        up = client.post('/v1/uploads', headers=headers(account), json={'filename': 'source.mp4', 'size_bytes': video.stat().st_size}).json()
        assert httpx.put(up['upload_url'], content=video.read_bytes(), headers={'Content-Type': 'video/mp4'}).status_code == 200
        assert client.post(f"/v1/jobs/{up['job_id']}/start", headers=headers(account),
            json={'options': {'segment': 'fixed', 'window': 4, 'max_clips': 1, 'max_frames': 4, 'max_side': 224,
                              'moments': 0, 'pose': False}}).status_code == 200
        job = jobs.claim('full-pipeline-test')
        worker._shutdown.clear()
        worker.process(job)
        result = client.get(f"/v1/jobs/{up['job_id']}", headers=headers(account)).json()
        assert result['status'] == 'succeeded', result
        report = client.get(f"/v1/jobs/{up['job_id']}/report", headers=headers(account)).json()
        assert httpx.get(report['report']['report_html_url']).status_code == 200
        usage = billing.summary(account['account_id'], job['id'])
        assert usage['requests'] == len(counter) >= 2
        assert usage['confirmed_usd'] == pytest.approx(len(counter) * 0.001)
        assert usage['reserved_usd'] == 0
        storage.purge_prefix(f"checkpoints/{account['account_id']}/{job['id']}/")
    finally:
        server.shutdown(); server.server_close(); thread.join()
