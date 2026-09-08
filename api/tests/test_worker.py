import json
import sys
import time
from dataclasses import replace

import pytest

from courtside_api import worker
from courtside_api.models import JobOptions
from .test_api import make_row


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    worker_settings = worker.settings()
    monkeypatch.setattr(worker, "settings", lambda: replace(
        worker_settings, work_dir=str(tmp_path), heartbeat_s=0.02, job_timeout_s=5))
    monkeypatch.setattr(worker.storage, "head", lambda *a: {"etag": "source"})
    monkeypatch.setattr(worker.checkpoints, "restore", lambda *a: None)
    monkeypatch.setattr(worker.billing, "summary", lambda *a: {"confirmed_usd": 0, "reserved_usd": 0})
    monkeypatch.setattr(worker.storage, "download", lambda key, dest, callback=None: None)
    monkeypatch.setattr(JobOptions, "to_argv", lambda *a: [
        sys.executable, "-c", "import time; time.sleep(0.5)", "--"])
    worker._shutdown.clear()
    yield tmp_path
    worker._shutdown.clear()


def test_silent_child_still_heartbeats(runtime, monkeypatch):
    beats = []
    monkeypatch.setattr(worker.jobs, "heartbeat", lambda *a, **kw: beats.append(kw) or True)
    job = make_row(status="running", worker_id="w", attempts=1)
    lease = worker.Lease(job)
    lease.thread.start()
    try:
        rc, _, _ = worker._run_pipeline(job, runtime, lease)
        assert rc == 0
        assert len(beats) >= 3
        assert beats[0]["worker_id"] == "w" and beats[0]["attempt"] == 1
    finally:
        lease.done.set()
        lease.thread.join()


@pytest.mark.parametrize("reason", ["timeout", "cancelled", "shutdown"])
def test_silent_child_is_interrupted(runtime, monkeypatch, reason):
    monkeypatch.setattr(JobOptions, "to_argv", lambda *a: [
        sys.executable, "-c", "import time; time.sleep(20)", "--"])
    lease = worker.Lease(make_row(worker_id="w", attempts=1))
    calls = 0
    original = lease.check
    def check(*a):
        nonlocal calls
        calls += 1
        if calls >= 3:
            if reason == "timeout":
                lease.deadline = 0
            elif reason == "shutdown":
                worker._shutdown.set()
            else:
                lease.reason = reason
        original()
    monkeypatch.setattr(lease, "check", check)
    started = time.monotonic()
    with pytest.raises(worker.JobStopped, match=reason):
        worker._run_pipeline(lease.job, runtime, lease)
    assert time.monotonic() - started < 4


def test_cleanup_failure_does_not_erase_success(runtime, monkeypatch):
    job = make_row(status="running", worker_id="w", attempts=1)
    doc = {"clips": [{"status": "ok"}, {"status": "failed"}], "facts": {}}
    def pipeline(job, work, lease):
        out = work / "out"
        out.mkdir()
        for name in ("session.json", "report.html", "session_report.md"):
            (out / name).write_text(json.dumps(doc))
        return 0, [], lease.progress
    results = []
    monkeypatch.setattr(worker, "_run_pipeline", pipeline)
    monkeypatch.setattr(worker, "_publish", lambda *a: ("reports/test", doc))
    monkeypatch.setattr(worker.jobs, "heartbeat", lambda *a, **k: True)
    monkeypatch.setattr(worker.jobs, "finish", lambda *a, **kw: results.append(kw) or dict(job, **kw))
    monkeypatch.setattr(worker.storage, "delete_prefix", lambda *a: (_ for _ in ()).throw(RuntimeError("S3 unavailable")))
    monkeypatch.setattr(worker.webhooks, "deliver", lambda *a: None)
    worker.process(job)
    assert len(results) == 1
    assert results[0]["status"] == "succeeded"
    assert results[0]["clips_total"] == 2 and results[0]["clips_done"] == 1


def test_missing_openrouter_key_fails_before_connecting_to_infrastructure(monkeypatch):
    config = replace(worker.settings(), openrouter_api_key="")
    monkeypatch.setattr(worker, "settings", lambda: config)
    def unexpected():
        pytest.fail("must reject a missing model key before bootstrapping the database")
    monkeypatch.setattr(worker.db, "bootstrap", unexpected)
    assert worker.main() == 2
