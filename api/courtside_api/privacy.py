"""Owner-authorized deletion requests and retryable storage/metadata retention."""
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from . import db, storage
from .config import settings


def _owned(c, job_id, principal):
    row = c.execute("SELECT * FROM jobs WHERE id = %s AND account_id = %s FOR UPDATE",
                    (job_id, principal.account_id)).fetchone()
    if not row or (principal.owner_id is not None and row["owner_id"] != principal.owner_id):
        raise HTTPException(404, "No such job.")
    return row


def request_delete(job_id, principal):
    with db.conn() as c, c.transaction():
        row = _owned(c, job_id, principal)
        if not row["deletion_requested_at"]:
            c.execute("""UPDATE jobs SET deletion_requested_at = now(), cancel_requested = true,
                status = CASE WHEN status IN ('awaiting_upload', 'queued') THEN 'cancelled' ELSE status END,
                finished_at = CASE WHEN status IN ('awaiting_upload', 'queued') THEN now() ELSE finished_at END
                WHERE id = %s""", (job_id,))
            c.execute("INSERT INTO deletion_audit(job_id, action) VALUES (%s, 'requested')", (job_id,))
    return deletion_status(job_id, principal)


def deletion_status(job_id, principal):
    with db.conn() as c, c.transaction():
        row = _owned(c, job_id, principal)
        return {"job_id": str(row["id"]), "requested_at": row["deletion_requested_at"],
                "deleted_at": row["deleted_at"], "upload_authorizations_expire_by": row["expires_at"] + timedelta(minutes=5) if row["expires_at"] else None,
                "state": "deleted" if row["deleted_at"] else
                         ("pending" if row["deletion_requested_at"] else "not_requested")}


def sweep() -> int:
    """Erase terminal data; repeat until all old signed PUTs have expired.

    Tombstones/usage/audit IDs remain for access control and billing. Backups are
    outside the live-store deletion domain and need the documented restore procedure.
    """
    s = settings()
    with db.conn() as c:
        c.execute("""UPDATE jobs SET deletion_requested_at = now()
            WHERE status IN ('succeeded', 'failed', 'cancelled', 'expired')
            AND deletion_requested_at IS NULL AND %s > 0 AND finished_at < now() - (%s * interval '1 day')""",
            (s.report_retention_days, s.report_retention_days))
        ids = [r["id"] for r in c.execute("""SELECT id FROM jobs WHERE
            status IN ('succeeded', 'failed', 'cancelled', 'expired') AND
            ((deletion_requested_at IS NOT NULL AND deleted_at IS NULL) OR
             (%s AND source_cleaned_at IS NULL))
            ORDER BY cleanup_attempted_at NULLS FIRST, id LIMIT 25""", (s.delete_source_after_analysis,)).fetchall()]
    done = 0
    for jid in ids:
        try:
            with db.conn() as c, c.transaction():
                row = c.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE SKIP LOCKED", (jid,)).fetchone()
                if not row or row["status"] not in ('succeeded', 'failed', 'cancelled', 'expired'):
                    continue
                aid = row["account_id"]
                storage.purge_prefix(f"uploads/{aid}/{jid}/")
                storage.purge_prefix(f"checkpoints/{aid}/{jid}/")
                if row["deletion_requested_at"]:
                    storage.purge_prefix(f"reports/{aid}/{jid}/")
                    c.execute("""UPDATE jobs SET filename = NULL, request_hash = NULL,
                        session_json = NULL, log_tail = NULL, error = NULL, webhook_url = NULL,
                        report_prefix = NULL, checkpoint_key = NULL WHERE id = %s""", (jid,))
                    c.execute("DELETE FROM webhook_outbox WHERE job_id = %s", (jid,))
                    c.execute("DELETE FROM webhook_deliveries WHERE job_id = %s", (jid,))
                # A previously issued signed PUT may create another object until expiry.
                if row["expires_at"] is None or row["expires_at"] + timedelta(minutes=5) <= db_now(c):
                    c.execute("""UPDATE jobs SET source_cleaned_at = now(),
                        deleted_at = CASE WHEN deletion_requested_at IS NOT NULL THEN now() ELSE deleted_at END
                        WHERE id = %s""", (jid,))
                    c.execute("INSERT INTO deletion_audit(job_id, action) VALUES (%s, %s)",
                              (jid, 'deleted' if row["deletion_requested_at"] else 'source_deleted'))
                    done += 1
        except Exception as e:
            with db.conn() as c:
                c.execute("INSERT INTO deletion_audit(job_id, action, error) VALUES (%s, 'retry', %s)",
                          (jid, type(e).__name__))
        finally:
            # A long upload window or storage failure must not starve later jobs.
            with db.conn() as c:
                c.execute("UPDATE jobs SET cleanup_attempted_at = now() WHERE id = %s", (jid,))
    return done


def db_now(c):
    return c.execute("SELECT now() AS t").fetchone()["t"]


def clean_scratch():
    root = Path(settings().work_dir)
    if not root.exists():
        return
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            jid = uuid.UUID(path.name)
        except ValueError:
            continue
        with db.conn() as c:
            row = c.execute("SELECT status FROM jobs WHERE id = %s", (jid,)).fetchone()
        if row and row["status"] in ('succeeded', 'failed', 'cancelled', 'expired'):
            shutil.rmtree(path)
