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


def deliver(job: dict, event: str = "job.completed") -> None:
    """Best-effort delivery with capped exponential backoff.

    Runs on the worker thread after the job row is already final, so a receiver
    that is down costs a slow tail on one job and never a lost result: the
    client can always poll GET /v1/jobs/{id}.
    """
    url = job.get("webhook_url")
    if not url:
        return
    s = settings()
    # Revalidate persisted jobs too; configuration may have changed since start.
    from .models import StartJobRequest
    StartJobRequest(webhook_url=url)
    body = json.dumps(build_payload(job, event), separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "courtside-webhooks/1"}
    headers[SIGNATURE_HEADER] = sign(body, s.webhook_secret)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    delivery_id = uuid.uuid4()
    with db.conn() as c:
        c.execute(
            "INSERT INTO webhook_deliveries (id, job_id, url, event) VALUES (%s, %s, %s, %s)",
            (delivery_id, job["id"], url, event),
        )

    last_error, code = None, None
    for attempt in range(1, s.webhook_max_attempts + 1):
        try:
            headers[SIGNATURE_HEADER] = sign(body, s.webhook_secret)
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with opener.open(req, timeout=s.webhook_timeout_s) as resp:
                code = resp.status
            if 200 <= (code or 0) < 300:
                _record(delivery_id, attempt, code, None, delivered=True)
                return
            last_error = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            code, last_error = e.code, f"HTTP {e.code}"
            if 400 <= e.code < 500 and e.code != 429:
                break                      # a 4xx will not become a 2xx on retry
        except Exception as e:             # noqa: BLE001 - network, DNS, TLS, timeouts
            last_error = f"{type(e).__name__}: {e}"
        if attempt < s.webhook_max_attempts:
            time.sleep(min(2 ** attempt, 30))

    log.warning("webhook delivery failed for job %s: %s", job["id"], last_error)
    _record(delivery_id, attempt, code, last_error, delivered=False)


def _record(delivery_id: uuid.UUID, attempts: int, code: int | None,
            error: str | None, *, delivered: bool) -> None:
    try:
        with db.conn() as c:
            c.execute(
                """UPDATE webhook_deliveries
                      SET attempts = %s, status_code = %s, error = %s, delivered_at = %s
                    WHERE id = %s""",
                (attempts, code, error, time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime())
                 if delivered else None, delivery_id),
            )
    except Exception:
        log.exception("could not record webhook delivery %s", delivery_id)
