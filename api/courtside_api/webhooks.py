"""Signed completion webhooks.

Stripe-style signing: the receiver recomputes HMAC-SHA256 over `{timestamp}.{body}`
and compares in constant time, rejecting timestamps outside a few minutes. That
gives the mobile backend both authenticity and replay protection without needing
a shared transport secret per customer.

    X-Courtside-Signature: t=1735689600,v1=<hex>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
import uuid

from . import db
from .config import settings

log = logging.getLogger("courtside.webhooks")

SIGNATURE_HEADER = "X-Courtside-Signature"
TOLERANCE_S = 300


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def verify(payload: bytes, header: str, secret: str, tolerance_s: int = TOLERANCE_S) -> bool:
    """Reference implementation of the check a receiver performs (also used in tests)."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def build_payload(job: dict, event: str) -> dict:
    return {
        "event": event,
        "job_id": str(job["id"]),
        "status": job["status"],
        "phase": job["phase"],
        "progress": job["progress"],
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "cost_usd": float(job["cost_usd"]) if job.get("cost_usd") is not None else None,
        "clips_total": job.get("clips_total"),
        "clips_done": job.get("clips_done"),
        "finished_at": job["finished_at"].isoformat() if job.get("finished_at") else None,
    }


def enqueue_pending() -> int:
    """Terminal job rows are the durable source; scan catches every crash window."""
    from psycopg.types.json import Jsonb
    with db.conn() as c, c.transaction():
        rows = c.execute("""SELECT j.* FROM jobs j WHERE j.status IN
            ('succeeded', 'failed', 'cancelled', 'expired') AND j.webhook_url IS NOT NULL
            AND j.deletion_requested_at IS NULL AND NOT EXISTS
            (SELECT 1 FROM webhook_outbox o WHERE o.job_id = j.id AND o.event = 'job.completed')
            LIMIT 100""").fetchall()
        for row in rows:
            c.execute("""INSERT INTO webhook_outbox(job_id, event, url, payload)
                VALUES (%s, 'job.completed', %s, %s) ON CONFLICT(job_id, event) DO NOTHING""",
                (row["id"], row["webhook_url"], Jsonb(build_payload(row, "job.completed"))))
    return len(rows)


def dispatch_one() -> bool:
    """Lease one event; retries survive restarts and success can be delivered twice."""
    s = settings()
    token = uuid.uuid4()
    with db.conn() as c, c.transaction():
        c.execute("""UPDATE webhook_outbox SET failed_at = now(), error = 'Retry budget exhausted'
            WHERE attempts >= %s AND delivered_at IS NULL AND failed_at IS NULL
            AND (lease_until IS NULL OR lease_until < now())""", (s.webhook_max_attempts,))
        row = c.execute("""UPDATE webhook_outbox SET lease_token = %s,
            lease_until = now() + interval '120 seconds', attempts = attempts + 1
            WHERE id = (SELECT id FROM webhook_outbox WHERE delivered_at IS NULL
                AND failed_at IS NULL AND next_attempt_at <= now()
                AND (lease_until IS NULL OR lease_until < now())
                ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""", (token,)).fetchone()
    if not row:
        return False
    code, error, delivered = None, None, False
    permanent = False
    try:
        from .models import StartJobRequest
        StartJobRequest(webhook_url=row["url"])
        payload = dict(row["payload"], delivery_id=str(row["id"]))
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "courtside-webhooks/1",
                   SIGNATURE_HEADER: sign(body, s.webhook_secret)}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        req = urllib.request.Request(row["url"], data=body, headers=headers, method="POST")
        with opener.open(req, timeout=min(s.webhook_timeout_s, 60)) as resp:
            code = resp.status
        delivered = 200 <= code < 300
        if not delivered:
            error = f"HTTP {code}"
    except urllib.error.HTTPError as e:
        code, error = e.code, f"HTTP {e.code}"
        permanent = 400 <= e.code < 500 and e.code not in (408, 429)
    except ValueError:
        error, permanent = "Callback is no longer allowed by configuration", True
    except Exception as e:
        error = type(e).__name__  # never persist callback URLs/query secrets in errors
    failed = permanent or row["attempts"] >= s.webhook_max_attempts
    with db.conn() as c:
        c.execute("""UPDATE webhook_outbox SET lease_until = NULL, lease_token = NULL,
            status_code = %s, error = %s,
            delivered_at = CASE WHEN %s THEN now() ELSE NULL END,
            failed_at = CASE WHEN %s AND NOT %s THEN now() ELSE NULL END,
            next_attempt_at = now() + (%s * interval '1 second')
            WHERE id = %s AND lease_token = %s""",
            (code, error, delivered, failed, delivered, min(3600, 2 ** min(row["attempts"], 12)), row["id"], token))
    return True


def deliver(job: dict, event: str = "job.completed") -> None:
    """Compatibility entrypoint: enqueue durable work, never block inference on HTTP."""
    enqueue_pending()
