"""Postgres access: one process-wide connection pool + schema bootstrap.

Sync psycopg3 throughout. The API's route handlers are plain `def`, so FastAPI
runs them in its threadpool and a sync pool is the simpler, sturdier choice --
the worker needs sync anyway (it blocks on a subprocess for up to an hour).
"""

from __future__ import annotations

import threading
import hashlib
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    settings().database_url,
                    min_size=1,
                    max_size=10,
                    kwargs={"row_factory": dict_row, "autocommit": True},
                    open=True,
                )
    return _pool


@contextmanager
def conn() -> Iterator["object"]:
    with pool().connection() as c:
        yield c


def _statements(sql: str) -> list[str]:
    """Split a .sql file into single statements.

    psycopg3 sends one statement per execute(), so the file has to be split, and
    a naive `sql.split(";")` is wrong twice over: a semicolon inside a `--`
    comment splits mid-comment, and a statement preceded by a comment gets
    dropped along with it. This walks the text instead, tracking single-quoted
    literals and line comments, so only a real statement terminator splits.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_comment = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_comment:
            if ch == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":          # '' is an escaped quote, not a terminator
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_comment = True
            i += 2
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def bootstrap(*, connection=None) -> None:
    """Apply ordered, checksummed migrations under a transaction/advisory lock.

    Version 001 adopts the original idempotent schema on existing deployments.
    Applied migrations are immutable; a checksum mismatch stops startup.
    """
    with (nullcontext(connection) if connection is not None else conn()) as c:
        # IF NOT EXISTS alone does not serialize concurrent catalog writes.
        # API processes and workers can all boot against an empty DB together.
        with c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(724390812)")
            c.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())""")
            files = [("001", SCHEMA_PATH)] + [
                (p.stem.split("_", 1)[0], p)
                for p in sorted(SCHEMA_PATH.with_name("migrations").glob("*.sql"))]
            for version, path in files:
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                applied = c.execute("SELECT checksum FROM schema_migrations WHERE version = %s",
                                    (version,)).fetchone()
                if applied:
                    if applied["checksum"] != checksum:
                        raise RuntimeError(f"Migration {version} checksum changed; restore the released file")
                    continue
                for stmt in _statements(sql):
                    c.execute(stmt)
                c.execute("INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                          (version, checksum))


def close() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def ping() -> bool:
    try:
        with conn() as c:
            c.execute("SELECT 1")
        return True
    except Exception:
        return False
