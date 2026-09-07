"""Real Postgres/S3 contract tests; only paid model inference is substituted.

Run against disposable infrastructure; see api/DEPLOYMENT.md.
"""
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

if os.environ.get("COURTSIDE_INTEGRATION") != "1":
    pytest.skip("requires disposable Postgres and S3", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import httpx
from courtside_api import auth, db, jobs, storage
from courtside_api.app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account(client):
    r = client.post("/v1/admin/accounts", headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]},
                    json={"name": "handoff-test", "monthly_usd_cap": 50})
    assert r.status_code == 201, r.text
    body = r.json()
    yield body
    storage.delete_prefix(f"uploads/{body['account_id']}/")
    storage.delete_prefix(f"reports/{body['account_id']}/")
    with db.conn() as c:
        c.execute("DELETE FROM accounts WHERE id = %s", (body["account_id"],))


def test_upload_auth_retry_report_and_key_revocation(client, account):
    headers = {"Authorization": f"Bearer {account['api_key']}"}
    assert client.get("/readyz").status_code == 200
    assert client.get("/v1/jobs").status_code == 401
    up = client.post("/v1/uploads", headers=headers,
                     json={"filename": "smoke.mp4", "size_bytes": 12}).json()
    # Actually use the signed URL; this catches internal-hostname/signing bugs.
    r = httpx.put(up["upload_url"], content=b"test payload", headers={"Content-Type": "video/mp4"})
    assert r.status_code == 200, r.text
    jid = up["job_id"]
    url = f"/v1/jobs/{jid}"
    assert client.post(url + "/start", headers=headers, json={"options": {"max_clips": 3}}).status_code == 200
    retry = client.post(url + "/start", headers=headers, json={"options": {"max_clips": 9}})
    assert retry.json()["options"]["max_clips"] == 3
    assert jobs.get(uuid.UUID(jid), uuid.uuid4()) is None
    row = jobs.claim("integration-worker")
    assert str(row["id"]) == jid
    out = jobs.finish(row["id"], worker_id=row["worker_id"], attempt=row["attempts"],
                      status="succeeded", phase="done", progress=100,
                      report_prefix=f"reports/{account['account_id']}/{jid}",
                      session_json={"facts": {"clips_analyzed": 2, "total_strokes": 7,
                                             "outcomes": {"errors": 3}}})
    assert out
    summary = client.get(url, headers=headers).json()["summary"]
    assert (summary["rallies"], summary["strokes"], summary["errors"]) == (2, 7, 3)
    revoked = client.delete(f"/v1/admin/keys/{account['key_id']}",
                            headers={"X-Admin-Token": os.environ["ADMIN_TOKEN"]})
    assert revoked.status_code == 204
    assert client.get(url, headers=headers).status_code == 401


def test_atomic_admission_and_old_attempt_cannot_finish(client, account):
    aid = uuid.UUID(account["account_id"])
    rows = [jobs.create(aid, filename="x.mp4", content_type="video/mp4",
                        video_key_fn=storage.video_key) for _ in range(6)]
    def start(row):
        try:
            return jobs.enqueue(row["id"], aid, {}, None)["status"]
        except jobs.AdmissionError as e:
            return e.status_code
    with ThreadPoolExecutor(max_workers=6) as ex:
        result = list(ex.map(start, rows))
    assert result.count("queued") == 3
    assert result.count(429) == 3
    first = jobs.claim("old-worker")
    with db.conn() as c:
        c.execute("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes' WHERE id = %s",
                  (first["id"],))
    jobs.requeue_stale(120)
    second = jobs.claim("new-worker")
    assert second["id"] == first["id"]
    assert second["attempts"] == first["attempts"] + 1
    assert jobs.finish(first["id"], worker_id="old-worker", attempt=first["attempts"],
                       status="failed", phase="failed", progress=0) is None
    assert jobs.get(first["id"])["worker_id"] == "new-worker"
    jobs.request_cancel(second["id"], aid)
    with db.conn() as c:
        c.execute("UPDATE jobs SET heartbeat_at = now() - interval '5 minutes' WHERE id = %s",
                  (second["id"],))
    jobs.requeue_stale(120)
    assert jobs.get(second["id"])["status"] == "cancelled"


def test_multipart_completion(client, account):
    headers = {"Authorization": f"Bearer {account['api_key']}"}
    up = client.post("/v1/uploads", headers=headers,
                     json={"filename": "part.mp4", "size_bytes": 8, "multipart": True}).json()
    r = httpx.put(up["parts"][0]["url"], content=b"testpart")
    assert r.status_code == 200
    body = {"parts": [{"part_number": 1, "etag": r.headers["etag"]}]}
    url = f"/v1/uploads/{up['job_id']}/complete"
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.post(url, headers=headers, json=body).status_code == 200
    assert client.post(f"/v1/jobs/{up['job_id']}/cancel", headers=headers).status_code == 200
