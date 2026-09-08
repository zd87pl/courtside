"""Portable per-clip checkpoints, fenced to the current worker attempt."""
import hashlib
import json
import os
import re
from pathlib import Path

from . import db, storage

FORMAT = 1


def pipeline_hash():
    import courtside
    root = Path(courtside.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def restore(job, work: Path, source_etag: str):
    context = {'format': FORMAT, 'job_id': str(job['id']), 'source_etag': source_etag,
               'options': job['options'], 'pipeline': pipeline_hash()}
    (work / 'checkpoint-context.json').write_text(json.dumps(context))
    key = job.get('checkpoint_key')
    if not key:
        return
    # Do not trust a modified database pointer to escape this job's prefix.
    prefix = f"checkpoints/{job['account_id']}/{job['id']}/"
    if not key.startswith(prefix):
        raise RuntimeError('Invalid checkpoint location')
    response = storage.client().get_object(Bucket=storage.bucket(), Key=key)
    try:
        raw = response['Body'].read(20 * 1024 * 1024 + 1)
    finally:
        response['Body'].close()
    if len(raw) > 20 * 1024 * 1024:
        raise RuntimeError('Checkpoint too large')
    checkpoint = json.loads(raw)
    if checkpoint['context'] != context:
        return  # source/options/code changed; do not reuse incompatible results
    out = work / 'out'
    out.mkdir(exist_ok=True)
    for name, value in checkpoint['files'].items():
        if not re.fullmatch(r'clip_\d{3,4}(?:\.stats)?\.json', name):
            raise RuntimeError('Invalid checkpoint filename')
        (out / name).write_text(json.dumps(value))


def save(out: Path):
    context = json.loads((out.parent / 'checkpoint-context.json').read_text())
    files = {}
    for path in sorted(out.glob('clip_*.json')):
        if re.fullmatch(r'clip_\d{3,4}(?:\.stats)?\.json', path.name):
            try:
                files[path.name] = json.loads(path.read_text())
            except ValueError:
                continue
    if not files:
        return
    jid, attempt, wid = os.environ['COURTSIDE_JOB_ID'], int(os.environ['COURTSIDE_JOB_ATTEMPT']), os.environ['COURTSIDE_JOB_WORKER']
    with db.conn() as c:
        row = c.execute("SELECT account_id FROM jobs WHERE id = %s AND worker_id = %s AND attempts = %s AND status = 'running' AND NOT cancel_requested",
                        (jid, wid, attempt)).fetchone()
    if not row:
        return
    # Never overwrite a checkpoint another attempt might be restoring.
    body = json.dumps({'context': context, 'files': files}).encode()
    key = f"checkpoints/{row['account_id']}/{jid}/attempt-{attempt}/{hashlib.sha256(body).hexdigest()}.json"
    storage.client().put_object(Bucket=storage.bucket(), Key=key, Body=body, ContentType='application/json')
    with db.conn() as c:
        c.execute("""UPDATE jobs SET checkpoint_key = %s WHERE id = %s AND worker_id = %s
            AND attempts = %s AND status = 'running' AND NOT cancel_requested""", (key, jid, wid, attempt))
