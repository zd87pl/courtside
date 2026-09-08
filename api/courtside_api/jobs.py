"""Job persistence and the queue.

The queue is Postgres: `SELECT ... FOR UPDATE SKIP LOCKED` hands one queued job
to exactly one worker, and a heartbeat column lets a reaper reclaim jobs whose
worker died mid-analysis. That is one fewer moving part than Redis for a
workload measured in jobs-per-hour, and it makes the job row the single source
of truth for both the API and the worker.

Status machine:

    awaiting_upload --(upload completed)--> queued --(claim)--> running
                  \\                                        /   |    \\
                   `--(never uploaded)--> expired          /    |     `--> failed
                                          succeeded <-----'     `--> cancelled
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from . import db, controls
from .config import settings

TERMINAL = ("succeeded", "failed", "cancelled", "expired")

_COLUMNS = """
    id, account_id, status, phase, progress, filename, content_type, video_key,
    video_bytes, multipart_id, options, webhook_url, error, error_code, cost_usd,
    video_duration_s, clips_total, clips_done, report_prefix, session_json,
    log_tail, attempts, max_attempts, worker_id, cancel_requested, created_at,
    queued_at, started_at, finished_at, heartbeat_at, expires_at, owner_id,
    request_hash, requested_bytes, upload_multipart, deletion_requested_at, deleted_at, checkpoint_key
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ create

def create(account_id: uuid.UUID, *, filename: str, content_type: str,
           video_key_fn, multipart_id: str | None = None, owner_id: str | None = None,
           idempotency_key: str | None = None, request_hash: str | None = None,
           requested_bytes: int | None = None, upload_multipart: bool = False) -> dict:
    job_id = uuid.uuid4()
    key = video_key_fn(account_id, job_id, filename)
    expires = _now() + timedelta(seconds=settings().upload_grace_s)
    with db.conn() as c, c.transaction():
        c.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,))
        if idempotency_key:
            old = c.execute(f"SELECT {_COLUMNS} FROM jobs WHERE account_id = %s AND idempotency_key = %s",
                            (account_id, idempotency_key)).fetchone()
            if old:
                if old["request_hash"] != request_hash or old["owner_id"] != owner_id:
                    raise AdmissionError(409, "Idempotency-Key was used for a different request or user.")
                if old["status"] != "awaiting_upload" or old["deletion_requested_at"] or old["expires_at"] <= _now():
                    raise AdmissionError(409, "Reservation is no longer uploadable; poll the original job.")
                return old
        pending = c.execute("SELECT count(*) AS n FROM jobs WHERE account_id = %s AND status = 'awaiting_upload' AND deletion_requested_at IS NULL",
                            (account_id,)).fetchone()["n"]
        if pending >= settings().max_pending_uploads:
            raise AdmissionError(429, "Too many pending upload reservations.")
        controls.rate(c, account_id, "uploads", settings().uploads_per_hour)
        return c.execute(f"""
            INSERT INTO jobs (id, account_id, filename, content_type, video_key,
                multipart_id, expires_at, owner_id, idempotency_key, request_hash,
                requested_bytes, upload_multipart)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}""",
            (job_id, account_id, filename, content_type, key, multipart_id, expires,
             owner_id, idempotency_key, request_hash, requested_bytes, upload_multipart)).fetchone()


def get(job_id: uuid.UUID, account_id: uuid.UUID | None = None) -> dict | None:
    sql = f"SELECT {_COLUMNS} FROM jobs WHERE id = %s AND deletion_requested_at IS NULL"
    params: list[Any] = [job_id]
    if account_id is not None:            # tenant isolation is enforced in SQL
        sql += " AND account_id = %s"
        params.append(account_id)
    with db.conn() as c:
        return c.execute(sql, tuple(params)).fetchone()


def list_for_account(account_id: uuid.UUID, *, limit: int = 25,
                     before: datetime | None = None, status: str | None = None, owner_id: str | None = None) -> list[dict]:
    sql = f"SELECT {_COLUMNS} FROM jobs WHERE account_id = %s AND deletion_requested_at IS NULL"
    params: list[Any] = [account_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    if status:
        sql += " AND status = %s"
        params.append(status)
    if before:
        sql += " AND created_at < %s"
        params.append(before)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with db.conn() as c:
        return list(c.execute(sql, tuple(params)).fetchall())


# ------------------------------------------------------------------ enqueue

def mark_uploaded(job_id: uuid.UUID, account_id: uuid.UUID, size: int | None) -> dict | None:
    """awaiting_upload -> awaiting_upload, with the observed object size recorded."""
    with db.conn() as c:
        return c.execute(
            f"""UPDATE jobs SET video_bytes = %s WHERE id = %s AND account_id = %s
                RETURNING {_COLUMNS}""",
            (size, job_id, account_id),
        ).fetchone()


class AdmissionError(ValueError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def enqueue(job_id: uuid.UUID, account_id: uuid.UUID, options: dict,
            webhook_url: str | None) -> dict | None:
    """Serialize admission per account; retries preserve the original request."""
    with db.conn() as c, c.transaction():
        account = c.execute(
            "SELECT monthly_usd_cap FROM accounts WHERE id = %s FOR UPDATE", (account_id,)
        ).fetchone()
        row = c.execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = %s AND account_id = %s FOR UPDATE",
                        (job_id, account_id)).fetchone()
        if row is None or row["status"] not in ("awaiting_upload", "queued"):
            return None
        if row["status"] == "queued":
            return row
        controls.rate(c, account_id, "starts", settings().starts_per_hour)
        usage = c.execute(
            """SELECT COUNT(*) FILTER (WHERE status IN ('queued', 'running')) AS active,
                      COALESCE(SUM(cost_usd) FILTER (WHERE finished_at >=
                        date_trunc('month', now(), 'UTC')), 0) AS spent
                 FROM jobs WHERE account_id = %s""", (account_id,)
        ).fetchone()
        charged = c.execute("""SELECT COALESCE(sum(COALESCE(actual_usd, reserved_usd)), 0) AS spent
            FROM model_usage WHERE account_id = %s AND created_at >= date_trunc('month', now(), 'UTC')""",
            (account_id,)).fetchone()["spent"]
        if charged >= account["monthly_usd_cap"]:
            raise AdmissionError(402, "Monthly spend plus unresolved reservations reached the account cap.")
        if usage["active"] >= settings().max_concurrent_jobs_per_account:
            raise AdmissionError(429, "Too many analyses in flight for this account.")
        return c.execute(
            f"""UPDATE jobs SET status = 'queued', queued_at = %s,
                    options = %s, webhook_url = %s, phase = 'queued', progress = 4
                WHERE id = %s AND account_id = %s AND status = 'awaiting_upload'
                RETURNING {_COLUMNS}""",
            (_now(), Jsonb(options), webhook_url, job_id, account_id),
        ).fetchone()


def request_cancel(job_id: uuid.UUID, account_id: uuid.UUID) -> dict | None:
    """Queued jobs cancel immediately; running jobs are flagged for the worker."""
    with db.conn() as c:
        return c.execute(
            f"""
            UPDATE jobs
               SET cancel_requested = true,
                   status = CASE WHEN status IN ('awaiting_upload', 'queued')
                                 THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status IN ('awaiting_upload', 'queued')
                                      THEN %s ELSE finished_at END
             WHERE id = %s AND account_id = %s AND status NOT IN {TERMINAL}
            RETURNING {_COLUMNS}
            """,
            (_now(), job_id, account_id),
        ).fetchone()


# ------------------------------------------------------------------- worker

def claim(worker_id: str) -> dict | None:
    """Atomically take one queued job. SKIP LOCKED makes N workers safe."""
    with db.conn() as c:
        return c.execute(
            f"""
            UPDATE jobs SET status = 'running', worker_id = %s, started_at = %s,
                            heartbeat_at = %s, attempts = attempts + 1,
                            phase = 'queued', progress = 4, error = NULL, error_code = NULL
             WHERE id = (
                   SELECT id FROM jobs
                    WHERE status = 'queued' AND cancel_requested = false
                    ORDER BY queued_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1)
            RETURNING {_COLUMNS}
            """,
            (worker_id, _now(), _now()),
        ).fetchone()


def heartbeat(job_id: uuid.UUID, *, worker_id: str, attempt: int, phase: str, progress: int, log_tail: str,
              clips_total: int | None, clips_done: int | None,
              video_duration_s: float | None) -> bool:
    """Stamp liveness + progress. Returns False once a cancel has been requested."""
    with db.conn() as c:
        row = c.execute(
            """
            UPDATE jobs SET heartbeat_at = %s, phase = %s, progress = %s, log_tail = %s,
                            clips_total = COALESCE(%s, clips_total),
                            clips_done = COALESCE(%s, clips_done),
                            video_duration_s = COALESCE(%s, video_duration_s)
             WHERE id = %s AND status = 'running' AND worker_id = %s AND attempts = %s
            RETURNING cancel_requested
            """,
            (_now(), phase, progress, log_tail, clips_total, clips_done,
             video_duration_s, job_id, worker_id, attempt),
        ).fetchone()
    return bool(row) and not row["cancel_requested"]


def finish(job_id: uuid.UUID, *, worker_id: str, attempt: int, status: str, phase: str, progress: int,
           error: str | None = None, error_code: str | None = None,
           cost_usd: float | None = None, session_json: dict | None = None,
           report_prefix: str | None = None, log_tail: str | None = None,
           clips_total: int | None = None, clips_done: int | None = None,
           video_duration_s: float | None = None) -> dict | None:
    with db.conn() as c:
        return c.execute(
            f"""
            UPDATE jobs SET status = %s, phase = %s, progress = %s, finished_at = %s,
                            error = %s, error_code = %s, cost_usd = %s,
                            session_json = %s, report_prefix = %s,
                            log_tail = COALESCE(%s, log_tail),
                            clips_total = COALESCE(%s, clips_total),
                            clips_done = COALESCE(%s, clips_done),
                            video_duration_s = COALESCE(%s, video_duration_s)
             WHERE id = %s AND status = 'running' AND worker_id = %s AND attempts = %s
            RETURNING {_COLUMNS}
            """,
            (status, phase, progress, _now(), error, error_code, cost_usd,
             Jsonb(session_json) if session_json is not None else None,
             report_prefix, log_tail, clips_total, clips_done, video_duration_s, job_id, worker_id, attempt),
        ).fetchone()


def requeue_stale(stale_after_s: int) -> int:
    """Reclaim jobs whose worker stopped heartbeating (OOM, machine replaced).

    Under max_attempts the job goes back on the queue; at the limit it fails
    loudly rather than looping forever on a video that kills workers.
    """
    cutoff = _now() - timedelta(seconds=stale_after_s)
    with db.conn() as c:
        c.execute(
            """UPDATE jobs SET status = 'cancelled', phase = 'cancelled', finished_at = %s
                 WHERE status = 'running' AND heartbeat_at < %s AND cancel_requested""",
            (_now(), cutoff),
        )
        back = c.execute(
            """
            UPDATE jobs SET status = 'queued', worker_id = NULL, phase = 'queued', progress = 4
             WHERE status = 'running' AND heartbeat_at < %s AND attempts < max_attempts AND cancel_requested = false
            RETURNING id
            """,
            (cutoff,),
        ).fetchall()
        dead = c.execute(
            """
            UPDATE jobs SET status = 'failed', finished_at = %s,
                            error = 'Worker stopped responding; the job exceeded its retry budget.',
                            error_code = 'worker_lost'
             WHERE status = 'running' AND heartbeat_at < %s AND attempts >= max_attempts
            RETURNING id
            """,
            (_now(), cutoff),
        ).fetchall()
    return len(back) + len(dead)


def expire_stale_uploads() -> int:
    """Drop jobs whose presigned upload was never used."""
    with db.conn() as c:
        rows = c.execute(
            """
            UPDATE jobs SET status = 'expired', finished_at = %s,
                            error = 'Upload was never completed.', error_code = 'upload_expired'
             WHERE status = 'awaiting_upload' AND expires_at < %s
            RETURNING id, video_key, multipart_id
            """,
            (_now(), _now()),
        ).fetchall()
    return len(rows)


# -------------------------------------------------------------------- usage

def month_usage(account_id: uuid.UUID) -> tuple[float, int]:
    """Confirmed provider cost plus unresolved reservations in the current UTC month."""
    with db.conn() as c:
        row = c.execute("""SELECT COALESCE(SUM(COALESCE(actual_usd, reserved_usd)), 0) AS spent,
            COUNT(DISTINCT job_id) AS n FROM model_usage WHERE account_id = %s
            AND created_at >= date_trunc('month', now(), 'UTC')""", (account_id,)).fetchone()
    return float(row["spent"]), int(row["n"])


def running_count(account_id: uuid.UUID) -> int:
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE account_id = %s AND status IN ('queued', 'running')",
            (account_id,),
        ).fetchone()
    return int(row["n"])
