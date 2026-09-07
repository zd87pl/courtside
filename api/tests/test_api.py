"""Route wiring, auth and status codes, with the DB and bucket stubbed out.

This is not an end-to-end test -- it proves the HTTP contract the mobile client
codes against: who gets 401, what a poll response looks like, and which
transitions are refused. The real end-to-end run needs Postgres and a bucket
(`docker compose -f api/docker-compose.yml up`).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from courtside_api import auth, jobs, storage
from courtside_api.app import app

ACCOUNT = uuid.uuid4()
PRINCIPAL = auth.Principal(account_id=ACCOUNT, account_name="Test Club",
                           key_id=uuid.uuid4(), monthly_usd_cap=50.0)


def make_row(**over):
    now = datetime.now(timezone.utc)
    row = {
        "id": uuid.uuid4(), "account_id": ACCOUNT, "status": "awaiting_upload",
        "phase": "queued", "progress": 4, "filename": "match.mp4",
        "content_type": "video/mp4", "video_key": "uploads/a/b/source.mp4",
        "video_bytes": None, "multipart_id": None, "options": {}, "webhook_url": None,
        "error": None, "error_code": None, "cost_usd": None, "video_duration_s": None,
        "clips_total": None, "clips_done": None, "report_prefix": None,
        "session_json": None, "log_tail": None, "attempts": 0, "max_attempts": 2,
        "worker_id": None, "cancel_requested": False, "created_at": now,
        "queued_at": None, "started_at": None, "finished_at": None,
        "heartbeat_at": None, "expires_at": now + timedelta(days=1),
    }
    row.update(over)
    return row


@pytest.fixture
def client(monkeypatch):
    app.dependency_overrides[auth.require_account] = lambda: PRINCIPAL
    monkeypatch.setattr(storage, "presign_put", lambda *a, **k: "https://bucket/put")
    monkeypatch.setattr(storage, "presign_get", lambda *a, **k: "https://bucket/get")
    monkeypatch.setattr(storage, "head", lambda key: {"size": 1024, "content_type": "video/mp4",
                                                      "etag": "x"})
    monkeypatch.setattr(jobs, "month_usage", lambda a: (0.0, 0))
    monkeypatch.setattr(jobs, "running_count", lambda a: 0)
    monkeypatch.setattr(jobs, "mark_uploaded", lambda j, a, s: make_row(id=j, video_bytes=s))
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ------------------------------------------------------------------- auth

def test_every_v1_route_requires_a_key():
    with TestClient(app) as c:                    # no dependency override here
        for method, path in [("post", "/v1/uploads"), ("get", "/v1/jobs"),
                             ("get", f"/v1/jobs/{uuid.uuid4()}"), ("get", "/v1/usage")]:
            r = c.post(path, json={}) if method == "post" else c.get(path)
            assert r.status_code == 401, path
            assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_admin_routes_reject_an_account_key():
    with TestClient(app) as c:
        r = c.post("/v1/admin/accounts", json={"name": "x"},
                   headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 403


# ---------------------------------------------------------------- uploads

def test_create_upload_returns_a_presigned_put(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "create", lambda *a, **k: row)
    r = client.post("/v1/uploads", json={"filename": "match.mp4"})
    assert r.status_code == 201
    body = r.json()
    assert body["job_id"] == str(row["id"])
    assert body["upload_url"] == "https://bucket/put"
    assert body["status"] == "awaiting_upload"
    assert body["parts"] is None


def test_multipart_upload_returns_per_part_urls(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "create", lambda *a, **k: row)
    monkeypatch.setattr(storage, "plan_multipart", lambda *a, **k: storage.MultipartPlan(
        upload_id="mp-1", part_size=32 << 20,
        urls=[{"part_number": 1, "url": "https://bucket/p1"},
              {"part_number": 2, "url": "https://bucket/p2"}]))
    monkeypatch.setattr("courtside_api.app.db.conn", _fake_conn)
    r = client.post("/v1/uploads", json={"filename": "m.mov", "multipart": True,
                                         "size_bytes": 50 << 20})
    assert r.status_code == 201
    assert r.json()["multipart_id"] == "mp-1"
    assert [p["part_number"] for p in r.json()["parts"]] == [1, 2]


def test_multipart_without_a_size_is_rejected(client, monkeypatch):
    monkeypatch.setattr(jobs, "create", lambda *a, **k: make_row())
    r = client.post("/v1/uploads", json={"filename": "m.mov", "multipart": True})
    assert r.status_code == 400


def test_an_oversized_video_is_refused_before_a_url_is_issued(client):
    r = client.post("/v1/uploads", json={"filename": "m.mov", "size_bytes": 99 * 1024**3})
    assert r.status_code == 413
    assert "720p" in r.json()["detail"]          # tells the client what to do instead


# ------------------------------------------------------------------- jobs

def test_start_queues_the_job(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    monkeypatch.setattr(jobs, "enqueue",
                        lambda j, a, o, w: make_row(id=row["id"], status="queued", options=o))
    r = client.post(f"/v1/jobs/{row['id']}/start", json={"options": {"max_clips": 3}})
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["options"]["max_clips"] == 3


@pytest.mark.parametrize("requested", [None, "vendor/custom-vision-model"])
def test_start_resolves_and_persists_the_selected_model(client, monkeypatch, requested):
    from dataclasses import replace
    from courtside_api.config import settings
    model = "qwen/qwen3.8-27b"
    monkeypatch.setattr("courtside_api.app.settings", lambda: config)
    config = replace(settings(), default_model=model)
    row = make_row()
    captured = {}
    monkeypatch.setattr(jobs, "get", lambda *a: row)
    def enqueue(j, a, options, webhook):
        captured.update(options)
        return make_row(id=j, status="queued", options=options)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    body = {"options": {"model": requested}} if requested else {}
    r = client.post(f"/v1/jobs/{row['id']}/start", json=body)
    assert r.status_code == 200
    assert captured["model"] == r.json()["options"]["model"] == (requested or model)


def test_start_is_refused_when_no_video_was_uploaded(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    monkeypatch.setattr(storage, "head", lambda key: None)
    r = client.post(f"/v1/jobs/{row['id']}/start", json={})
    assert r.status_code == 409
    assert "Complete the upload" in r.json()["detail"]


def test_start_is_refused_over_the_monthly_cap(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    monkeypatch.setattr(jobs, "month_usage", lambda a: (50.0, 12))
    r = client.post(f"/v1/jobs/{row['id']}/start", json={})
    assert r.status_code == 402


def test_start_is_refused_when_too_many_are_in_flight(client, monkeypatch):
    row = make_row()
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    monkeypatch.setattr(jobs, "running_count", lambda a: 3)
    r = client.post(f"/v1/jobs/{row['id']}/start", json={})
    assert r.status_code == 429


def test_a_running_job_reports_phase_progress_and_an_eta(client, monkeypatch):
    row = make_row(status="running", phase="analyzing", progress=50,
                   started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                   clips_total=146, clips_done=73, video_duration_s=2416.6)
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    body = client.get(f"/v1/jobs/{row['id']}").json()
    assert body["phase"] == "analyzing" and body["progress"] == 50
    assert body["summary"]["clips_done"] == 73
    assert 500 < body["eta_s"] < 700          # ~10 more minutes at half done


def test_no_eta_before_there_is_anything_to_extrapolate_from(client, monkeypatch):
    row = make_row(status="running", progress=4, started_at=datetime.now(timezone.utc))
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    assert client.get(f"/v1/jobs/{row['id']}").json()["eta_s"] is None


def test_a_succeeded_job_carries_signed_artifact_urls(client, monkeypatch):
    row = make_row(status="succeeded", phase="done", progress=100,
                   report_prefix="reports/a/b", cost_usd=5.17,
                   session_json={"facts": {"rallies": 146, "strokes": 812}})
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    body = client.get(f"/v1/jobs/{row['id']}").json()
    assert body["report"]["report_html_url"] == "https://bucket/get"
    assert body["summary"]["cost_usd"] == 5.17
    assert body["summary"]["rallies"] == 146


def test_an_unfinished_job_exposes_no_report_urls(client, monkeypatch):
    row = make_row(status="running", report_prefix="reports/a/b")
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    assert client.get(f"/v1/jobs/{row['id']}").json()["report"]["report_html_url"] is None


def test_report_endpoint_is_409_until_the_job_succeeds(client, monkeypatch):
    row = make_row(status="running")
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    assert client.get(f"/v1/jobs/{row['id']}/report").status_code == 409


def test_artifact_route_redirects_to_a_signed_url(client, monkeypatch):
    row = make_row(status="succeeded", report_prefix="reports/a/b")
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    r = client.get(f"/v1/jobs/{row['id']}/report/report.html", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://bucket/get"


def test_unknown_artifacts_are_404(client, monkeypatch):
    row = make_row(status="succeeded", report_prefix="reports/a/b")
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    assert client.get(f"/v1/jobs/{row['id']}/report/../../etc/passwd").status_code == 404


def test_a_job_belonging_to_another_account_is_404_not_403(client, monkeypatch):
    # jobs.get filters on account_id in SQL, so a foreign id simply returns None
    # and the caller cannot distinguish "not yours" from "does not exist".
    monkeypatch.setattr(jobs, "get", lambda j, a=None: None)
    assert client.get(f"/v1/jobs/{uuid.uuid4()}").status_code == 404


def test_a_malformed_job_id_is_404_not_500(client):
    assert client.get("/v1/jobs/not-a-uuid").status_code == 404


def test_cancel_on_a_finished_job_is_409(client, monkeypatch):
    row = make_row(status="succeeded")
    monkeypatch.setattr(jobs, "request_cancel", lambda j, a: None)
    monkeypatch.setattr(jobs, "get", lambda j, a=None: row)
    r = client.post(f"/v1/jobs/{row['id']}/cancel")
    assert r.status_code == 409 and "succeeded" in r.json()["detail"]


def test_listing_omits_signed_urls(client, monkeypatch):
    rows = [make_row(status="succeeded", report_prefix="reports/a/b") for _ in range(3)]
    monkeypatch.setattr(jobs, "list_for_account", lambda *a, **k: rows)
    body = client.get("/v1/jobs?limit=3").json()
    assert len(body["jobs"]) == 3
    assert all(j["report"]["report_html_url"] is None for j in body["jobs"])
    assert body["next_cursor"] is not None       # a full page hands back a cursor


def test_usage_reports_spend_against_the_cap(client, monkeypatch):
    monkeypatch.setattr(jobs, "month_usage", lambda a: (12.5, 4))
    body = client.get("/v1/usage").json()
    assert body["spent_usd"] == 12.5 and body["remaining_usd"] == 37.5
    assert body["jobs_run"] == 4


# --------------------------------------------------------------- plumbing

class _FakeCursor:
    def fetchone(self): return None
    def fetchall(self): return []


class _FakeConn:
    def execute(self, *a, **k): return _FakeCursor()
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_conn():
    return _FakeConn()


def test_healthz_is_open(client):
    assert client.get("/healthz").json()["ok"] is True


def test_openapi_documents_the_whole_v1_surface(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/v1/uploads", "/v1/jobs/{job_id}", "/v1/jobs/{job_id}/start",
              "/v1/jobs/{job_id}/report", "/v1/usage"):
        assert p in paths


def test_queued_start_retry_preserves_options_even_at_cap(client, monkeypatch):
    row = make_row(status="queued", options={"max_clips": 3})
    monkeypatch.setattr(jobs, "get", lambda *a: row)
    monkeypatch.setattr(jobs, "running_count", lambda *a: 3)
    body = client.post(f"/v1/jobs/{row['id']}/start", json={"options": {"max_clips": 10}}).json()
    assert body["options"]["max_clips"] == 3


def test_pipeline_fact_names_map_to_mobile_summary(client, monkeypatch):
    row = make_row(session_json={"facts": {"clips_analyzed": 4, "total_strokes": 23,
                                          "outcomes": {"errors": 2}}})
    monkeypatch.setattr(jobs, "get", lambda *a: row)
    summary = client.get(f"/v1/jobs/{row['id']}").json()["summary"]
    assert (summary["rallies"], summary["strokes"], summary["errors"]) == (4, 23, 2)


@pytest.mark.parametrize("url", ["https://localhost/hook", "https://127.0.0.1/",
    "https://example.com@evil.test/", "https://example.com:444/", "https://example.com/#fragment"])
def test_untrusted_callback_urls_rejected(client, url):
    r = client.post(f"/v1/jobs/{uuid.uuid4()}/start", json={"webhook_url": url})
    assert r.status_code == 422
