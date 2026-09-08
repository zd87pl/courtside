#!/usr/bin/env python3
"""Back up Postgres or restore into an empty, separate database with erasures replayed.

Requires pg_dump/pg_restore and psycopg. Database URLs come from environment only.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import psycopg
from psycopg.rows import dict_row


def run_pg(command, url, *, output=None, source_file=None):
    fields = psycopg.conninfo.conninfo_to_dict(url)
    if not fields.get('dbname'):
        raise RuntimeError('Database URL must name a database')
    mapping = {'dbname': 'PGDATABASE', 'host': 'PGHOST', 'port': 'PGPORT', 'user': 'PGUSER',
               'password': 'PGPASSWORD', 'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT'}
    env = dict(os.environ)
    for name, variable in mapping.items():
        if name in fields:
            env[variable] = fields[name]
    if command[0] == 'pg_restore':
        command = command + ['--dbname', fields['dbname']]
    if env.get('PG_CONTAINER'):
        command = ['docker', 'exec', '-i'] + [arg for variable in mapping.values()
                   if variable in env for arg in ('-e', variable)] + [env['PG_CONTAINER']] + command
    result = subprocess.run(command, env=env, stdin=source_file,
                            stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Postgres tool failed; database/credential details withheld')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'export-deletions', 'restore'])
    parser.add_argument('file', type=Path)
    parser.add_argument('--deletions', type=Path, help='Fresh deletion manifest from the current source database; required for restore')
    args = parser.parse_args()
    os.umask(0o077)
    source = os.environ.get('DATABASE_URL')
    if args.action == 'restore':
        target = os.environ.get('RESTORE_DATABASE_URL')
        if not target or not args.deletions:
            parser.error('Set RESTORE_DATABASE_URL and provide --deletions before restoring')
        if source and psycopg.conninfo.conninfo_to_dict(source) == psycopg.conninfo.conninfo_to_dict(target):
            parser.error('Restore target must differ from the source database')
        ids = [uuid.UUID(v) for v in json.loads(args.deletions.read_text())['deleted_job_ids']]
        with psycopg.connect(target) as c:
            count = c.execute("SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')").fetchone()[0]
            if count:
                raise RuntimeError('Restore target must be an empty disposable database')
        with args.file.open('rb') as dump:
            run_pg(['pg_restore', '--no-owner', '--no-privileges', '--exit-on-error', '--single-transaction'], target, source_file=dump)
        # A backup may predate the erasure columns. Upgrade only the isolated
        # target before replaying tombstones; never open it to traffic in between.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from courtside_api import db
        with psycopg.connect(target, row_factory=dict_row) as c:
            db.bootstrap(connection=c)
            c.execute("""UPDATE jobs SET deletion_requested_at = COALESCE(deletion_requested_at, now()),
                deleted_at = NULL, cancel_requested = true, status = 'cancelled', finished_at = now(),
                filename = NULL, session_json = NULL, log_tail = NULL, error = NULL,
                webhook_url = NULL, report_prefix = NULL, checkpoint_key = NULL
                WHERE id = ANY(%s)""", (ids,))
            c.execute("INSERT INTO deletion_audit(job_id, action) SELECT id, 'restore_erasure' FROM jobs WHERE id = ANY(%s)", (ids,))
            c.execute('DELETE FROM webhook_outbox WHERE job_id = ANY(%s)', (ids,))
            c.execute('DELETE FROM webhook_deliveries WHERE job_id = ANY(%s)', (ids,))
        print('Restored into an empty database, applied migrations, and replayed deletion tombstones. Run acceptance checks before serving traffic.')
    elif not source:
        parser.error('Set DATABASE_URL')
    elif args.action == 'backup':
        if args.file.exists():
            raise RuntimeError('Backup output already exists; choose a new path')
        with args.file.open('xb') as dump:
            run_pg(['pg_dump', '--format=custom', '--no-owner', '--no-privileges'], source, output=dump)
        args.file.chmod(0o600)
        print('Backup written. Store it encrypted with access controls and an expiry policy.')
    else:
        with psycopg.connect(source) as c:
            rows = c.execute('SELECT id FROM jobs WHERE deletion_requested_at IS NOT NULL').fetchall()
        with args.file.open('x') as out:
            json.dump({'deleted_job_ids': [str(row[0]) for row in rows]}, out)
        print('Deletion manifest exported; retain it separately from historical backups.')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        raise SystemExit(f'Database operation failed ({type(e).__name__}); check configuration. No credentials printed.') from None
