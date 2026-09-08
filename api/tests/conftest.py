"""Env for the pure-logic suite.

These tests deliberately touch no Postgres and no S3: everything they cover
(argv rendering, progress parsing, key hashing, webhook signing, part sizing) is
the part that must be right before any infrastructure exists. Set the required
vars so `settings()` constructs.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "postgres://test:test@localhost:5432/test")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("S3_BUCKET", "courtside-test")
os.environ.setdefault("WEBHOOK_ALLOWED_HOSTS", "example.com,hooks.example.com")


import pytest


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    """Neutralise startup DB work for the logic suite.

    TestClient's context manager runs the app lifespan, which bootstraps the
    schema against a real Postgres. These tests stub the data layer instead, so
    the connection attempt would only hang.
    """
    from courtside_api import db
    def no_connection():
        pytest.fail("Unit test attempted a real database connection; stub the boundary or use integration tests")
    monkeypatch.setattr(db, "conn", no_connection)
    monkeypatch.setattr(db, "bootstrap", lambda: None)
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setattr(db, "ping", lambda: True)
