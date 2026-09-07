"""The schema splitter.

psycopg sends one statement per execute(), so schema.sql must be split before
it is applied. Getting this wrong is silent: a dropped CREATE TABLE surfaces
much later as a missing-relation error, so the splitter is tested directly.
"""

from courtside_api.db import SCHEMA_PATH, _statements

SCHEMA = _statements(SCHEMA_PATH.read_text())


def test_a_semicolon_inside_a_comment_does_not_split():
    stmts = _statements("-- note; with a semicolon\nSELECT 1;")
    assert stmts == ["SELECT 1"]


def test_a_statement_preceded_by_a_comment_survives():
    stmts = _statements("-- explain the table\nCREATE TABLE t (a int);\n-- next\nSELECT 2;")
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE t")
    assert stmts[1] == "SELECT 2"


def test_a_semicolon_inside_a_string_literal_does_not_split():
    stmts = _statements("INSERT INTO t VALUES ('a;b');")
    assert stmts == ["INSERT INTO t VALUES ('a;b')"]


def test_escaped_quotes_inside_a_literal_are_handled():
    stmts = _statements("INSERT INTO t VALUES ('it''s; fine');SELECT 1;")
    assert stmts == ["INSERT INTO t VALUES ('it''s; fine')", "SELECT 1"]


def test_a_trailing_statement_without_a_semicolon_is_kept():
    assert _statements("SELECT 1") == ["SELECT 1"]


def test_blank_input_yields_nothing():
    assert _statements("\n\n-- only a comment\n") == []


def test_the_real_schema_creates_every_table_and_index():
    joined = " ".join(SCHEMA)
    for table in ("accounts", "api_keys", "jobs", "webhook_deliveries"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined
    for index in ("api_keys_account_idx", "jobs_account_idx", "jobs_queue_idx",
                  "jobs_idempotency_idx", "webhook_job_idx"):
        assert index in joined


def test_every_schema_statement_is_idempotent():
    # bootstrap() runs on every boot of every machine; nothing may fail the
    # second time.
    for stmt in SCHEMA:
        assert "IF NOT EXISTS" in stmt.upper(), stmt[:60]


def test_no_statement_is_a_bare_comment_fragment():
    for stmt in SCHEMA:
        assert not stmt.startswith("--")
        assert stmt.upper().startswith("CREATE")
