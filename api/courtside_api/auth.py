"""API-key authentication.

Keys look like `cs_live_<43 url-safe chars>`. Only the SHA-256 hash is stored,
so a database leak does not hand over working credentials; the `prefix` column
keeps keys identifiable in a dashboard without storing the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status

from . import db
from .config import settings

KEY_PREFIX = "cs_live_"
PREFIX_LEN = len(KEY_PREFIX) + 8


@dataclass(frozen=True)
class Principal:
    account_id: uuid.UUID
    account_name: str
    key_id: uuid.UUID
    monthly_usd_cap: float


def generate_key() -> tuple[str, str, str]:
    """-> (plaintext key, prefix for display, sha-256 hash for storage)."""
    secret = secrets.token_urlsafe(32)
    key = f"{KEY_PREFIX}{secret}"
    return key, key[:PREFIX_LEN], hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("Missing Authorization header.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Expected 'Authorization: Bearer <api key>'.")
    return token.strip()


def require_account(authorization: str | None = Header(default=None)) -> Principal:
    """FastAPI dependency: resolve a bearer key to its account."""
    key = _bearer(authorization)
    with db.conn() as c:
        row = c.execute(
            """
            SELECT k.id AS key_id, k.account_id, a.name, a.monthly_usd_cap, a.disabled_at
              FROM api_keys k JOIN accounts a ON a.id = k.account_id
             WHERE k.key_hash = %s AND k.revoked_at IS NULL
            """,
            (hash_key(key),),
        ).fetchone()
        if row is None:
            raise _unauthorized("Invalid or revoked API key.")
        if row["disabled_at"] is not None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is disabled.")
        # Best-effort last-used stamp; never fail a request over telemetry.
        try:
            c.execute("UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                      (datetime.now(timezone.utc), row["key_id"]))
        except Exception:
            pass
    return Principal(
        account_id=row["account_id"],
        account_name=row["name"],
        key_id=row["key_id"],
        monthly_usd_cap=float(row["monthly_usd_cap"]),
    )


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Gate for /v1/admin/*. Constant-time compare; no timing oracle on the token."""
    expected = settings().admin_token
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin token required.")


AccountDep = Depends(require_account)
AdminDep = Depends(require_admin)
