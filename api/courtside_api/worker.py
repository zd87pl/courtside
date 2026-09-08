"""The analysis worker.

One loop, N slots. Each slot claims a job, pulls the video from object storage,
shells out to the `courtside` CLI with OpenRouter as the VLM backend, streams
the child's stdout into progress updates, then uploads the report artifacts.

Shelling out rather than importing the pipeline is deliberate: it is the same
contract the local web UI already uses, a wedged analysis can be killed with a
signal, and an ffmpeg or model crash takes down a child process rather than the
worker. The child inherits OPENROUTER_API_KEY through its environment and never
sees it on argv, where it would show up in `ps`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import db, jobs, storage, webhooks, billing, checkpoints, privacy
from .config import settings
from .models import JobOptions
from .phases import Progress

log = logging.getLogger("courtside.worker")

LOG_TAIL_LINES = 400
ARTIFACTS = (
    ("session.json", "application/json"),
    ("report.html", "text/html; charset=utf-8"),
    ("session_report.md", "text/markdown; charset=utf-8"),
    ("court_anchor.jpg", "image/jpeg"),
)

_shutdown = threading.Event()


# ------------------------------------------------------------------ helpers

def _tail(lines: list[str], n: int = LOG_TAIL_LINES) -> str:
    return "\n".join(lines[-n:])


def _cost_from_session(doc: dict) -> float | None:
    stats = doc.get("run_stats") or {}
    val = stats.get("cloud_equiv_usd")
    return float(val) if isinstance(val, (int, float)) else None


def _summary_from_session(doc: dict) -> dict:
    facts = doc.get("facts") or {}
    clips = doc.get("clips") or []
    return {
        "video_duration_s": doc.get("video_duration_s"),
        "clips_total": len(clips) or None,
        "clips_done": sum(c.get("status") == "ok" for c in clips),
        "rallies": facts.get("rallies"),
        "strokes": facts.get("strokes"),
    }


def _slim_session(doc: dict) -> dict:
    """Store the session document without the per-clip frame payloads.

    The full document for a 146-clip match is megabytes; the API only ever
    serves it as a summary, and the complete file is a presigned download away.
    """
    slim = {k: v for k, v in doc.items() if k != "clips"}
    slim["clips"] = [
        {k: v for k, v in c.items() if k not in ("frames", "raw", "raw_repair")}
        for c in (doc.get("clips") or [])
    ]
    return slim


# ------------------------------------------------------------- job execution

class JobStopped(RuntimeError):
    pass


class Lease:
    """Heartbeat independently of stdout, including during S3 transfers."""
    def __init__(self, job: dict):
        self.job = job
        self.progress = Progress()
        self.lines: list[str] = []
        self.done = threading.Event()
        self.reason: str | None = None
        self.deadline = time.monotonic() + settings().job_timeout_s
        self.thread = threading.Thread(target=self._monitor, daemon=True)

    def _monitor(self):
        while not self.done.is_set():
            try:
                phase, pct = self.progress.snapshot("running")
                alive = jobs.heartbeat(
                    self.job["id"], worker_id=self.job["worker_id"], attempt=self.job["attempts"],
                    phase=phase, progress=pct, log_tail=_tail(self.lines),
                    clips_total=self.progress.clips_total, clips_done=self.progress.clips_done,
                    video_duration_s=self.progress.video_duration_s,
                )
                if not alive:
                    self.reason = "cancelled"
                    return
            except Exception:
                # Stop work on a lost database connection; never continue billing
                # while another worker may reclaim the lease.
                log.exception("heartbeat failed for job %s", self.job["id"])
                self.reason = "lease_lost"
                return
            if self.done.wait(settings().heartbeat_s):
                return

    def check(self, *_):
        if _shutdown.is_set():
            raise JobStopped("shutdown")
        if self.reason:
            raise JobStopped(self.reason)
        if time.monotonic() >= self.deadline:
            raise JobStopped("timeout")


def _run_pipeline(job: dict, work: Path, lease: Lease) -> tuple[int, list[str], Progress]:
    s = settings()
    opts = JobOptions.model_validate(job["options"] or {})
    video = work / "source" / Path(job["video_key"]).name
    out_dir = work / "out"
    before = storage.head(job["video_key"])
    storage.download(job["video_key"], video, callback=lease.check)
    after = storage.head(job["video_key"])
    if not before or not after or before["etag"] != after["etag"]:
        raise RuntimeError("Source changed during download")
    checkpoints.restore(job, work, after["etag"])
    lease.check()
    argv = opts.to_argv(str(video), str(out_dir), s.openrouter_url, s.default_model)
    argv.insert(argv.index("--"), "--resume")
    env = dict(os.environ)
    env["OPENROUTER_API_KEY"] = s.openrouter_api_key
    env["PYTHONUNBUFFERED"] = "1"
    env["COURTSIDE_JOB_ID"] = str(job["id"])
    env["COURTSIDE_JOB_ATTEMPT"] = str(job["attempts"])
    env["COURTSIDE_JOB_WORKER"] = job["worker_id"]
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace", bufsize=1, env=env, cwd=str(work), start_new_session=True,
    )
    output: queue.Queue = queue.Queue(maxsize=1000)
    reader_done = threading.Event()

    def read_output():
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                while not reader_done.is_set():
                    try:
                        output.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            reader_done.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        while not reader_done.is_set() or not output.empty() or proc.poll() is None:
            lease.check()
            try:
                line = output.get(timeout=0.2)
            except queue.Empty:
                continue
            lease.lines.append(line.rstrip("\n"))
            if len(lease.lines) > 5000:
                del lease.lines[:len(lease.lines) - 5000]
            lease.progress.feed(line)
        lease.check()
        return proc.wait(), lease.lines, lease.progress
    finally:
        # Includes timeout, cancellation, shutdown, and unexpected exceptions.
        _kill(proc)
        reader_done.set()
        reader.join(timeout=2)
        if proc.stdout:
            proc.stdout.close()


def _kill(proc: subprocess.Popen) -> None:
    """TERM the process group, then KILL. ffmpeg children must not outlive us."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _publish(job: dict, out_dir: Path, lease: Lease) -> tuple[str, dict]:
    """Upload the report artifacts; return (prefix, session document)."""
    prefix = storage.report_prefix(job["account_id"], job["id"]) + f"/attempt-{job['attempts']}"
    session_path = out_dir / "session.json"
    doc = json.loads(session_path.read_text()) if session_path.is_file() else {}

    doc["_artifacts"] = []
    for name, ctype in ARTIFACTS:
        p = out_dir / name
        if p.is_file():
            storage.upload(p, f"{prefix}/{name}", ctype, callback=lease.check)
            doc["_artifacts"].append(name)
    return prefix, doc


def process(job: dict) -> None:
    job_id = job["id"]
    work = Path(settings().work_dir) / str(job_id) / f"attempt-{job['attempts']}"
    work.mkdir(parents=True, exist_ok=True)
    lease = Lease(job)
    final = None

    def finish(**kwargs):
        usage = billing.summary(job["account_id"], job_id)
        kwargs["cost_usd"] = usage["confirmed_usd"] + usage["reserved_usd"]
        return jobs.finish(job_id, worker_id=job["worker_id"], attempt=job["attempts"], **kwargs)

    log.info("job %s: starting (attempt %s)", job_id, job["attempts"])
    lease.thread.start()
    try:
        rc, lines, progress = _run_pipeline(job, work, lease)
        out_dir = work / "out"
        required = ("session.json", "report.html", "session_report.md")
        if rc != 0 or not all((out_dir / name).is_file() for name in required):
            final = finish(
                status="failed", phase="failed", progress=progress.pct,
                error=_failure_reason(lines, rc), error_code="analysis_failed",
                log_tail=_tail(lines), clips_total=progress.clips_total,
                clips_done=progress.clips_done, video_duration_s=progress.video_duration_s,
            )
        else:
            prefix, doc = _publish(job, out_dir, lease)
            lease.check()
            summary = _summary_from_session(doc)
            final = finish(
                status="succeeded", phase="done", progress=100,
                cost_usd=_cost_from_session(doc), session_json=_slim_session(doc),
                report_prefix=prefix, log_tail=_tail(lines),
                clips_total=summary["clips_total"] or progress.clips_total,
                clips_done=summary["clips_done"],
                video_duration_s=summary["video_duration_s"] or progress.video_duration_s,
            )
    except JobStopped as e:
        reason = str(e)
        if reason in ("shutdown", "lease_lost"):
            log.info("job %s interrupted (%s); lease will expire for retry", job_id, reason)
        else:
            state = "cancelled" if reason == "cancelled" else "failed"
            final = finish(status=state, phase=state, progress=lease.progress.pct,
                           error="Cancelled." if state == "cancelled" else "Job exceeded JOB_TIMEOUT_S.",
                           error_code=reason, log_tail=_tail(lease.lines))
    except Exception:
        log.exception("job %s crashed", job_id)
        final = finish(status="failed", phase="failed", progress=lease.progress.pct,
                       error="Worker error. Contact the service operator with the job ID.",
                       error_code="worker_error")
    finally:
        lease.done.set()
        lease.thread.join(timeout=5)
        shutil.rmtree(work, ignore_errors=True)

    if final:
        # A cleanup failure must never turn a successful report into a failed job.
        if settings().delete_source_after_analysis:
            try:
                if job.get("multipart_id"):
                    storage.abort_multipart(job["video_key"], job["multipart_id"])
                storage.delete_prefix(str(Path(job["video_key"]).parent) + "/")
            except Exception:
                log.exception("source cleanup failed for job %s; bucket lifecycle must expire it", job_id)
        try:
            webhooks.deliver(final)
        except Exception:
            log.exception("webhook dispatch failed for job %s", job_id)
    log.info("job %s: %s", job_id, (final or {}).get("status", "interrupted"))


def _failure_reason(lines: list[str], rc: int) -> str:
    """Surface the pipeline's own error line rather than a bare exit code."""
    for line in reversed(lines[-80:]):
        s = line.strip()
        if s.startswith("error:") or s.startswith("runner error:"):
            return s[:1000]
    if rc < 0:
        return f"Analysis was killed by signal {-rc}."
    return f"Analysis exited with status {rc}."


# ------------------------------------------------------------------- runloop

def _slot(worker_id: str) -> None:
    s = settings()
    while not _shutdown.is_set():
        try:
            job = jobs.claim(worker_id)
        except Exception:
            log.exception("claim failed")
            _shutdown.wait(s.poll_interval_s * 3)
            continue
        if job is None:
            _shutdown.wait(s.poll_interval_s)
            continue
        try:
            process(job)
        except Exception:
            # A storage/DB outage while finalizing must not kill this slot.
            log.exception("job processing interrupted; stale lease will be reclaimed")


def _janitor() -> None:
    s = settings()
    while not _shutdown.is_set():
        try:
            n = jobs.requeue_stale(s.stale_after_s)
            if n:
                log.info("janitor: reclaimed %s stale job(s)", n)
            jobs.expire_stale_uploads()
            webhooks.enqueue_pending()
            privacy.sweep()
            privacy.clean_scratch()
            billing.reconcile()
            with db.conn() as c:
                c.execute("DELETE FROM rate_limits WHERE bucket < floor(extract(epoch from now()) / 3600) - 48")
        except Exception:
            log.exception("janitor pass failed")
        _shutdown.wait(60)


def _telemetry(worker_id):
    while not _shutdown.is_set():
        try:
            with db.conn() as c:
                c.execute("""INSERT INTO workers(id, slots) VALUES (%s, %s)
                    ON CONFLICT(id) DO UPDATE SET slots = excluded.slots, heartbeat_at = now()""",
                    (worker_id, settings().worker_concurrency))
                c.execute("DELETE FROM workers WHERE heartbeat_at < now() - interval '7 days'")
        except Exception:
            log.exception("worker telemetry failed")
        _shutdown.wait(15)


def _maintenance():
    while not _shutdown.is_set():
        try:
            webhooks.enqueue_pending()
            for _ in range(10):
                if _shutdown.is_set() or not webhooks.dispatch_one():
                    break
        except Exception:
            log.exception("worker telemetry/outbox pass failed")
        _shutdown.wait(5)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    s = settings()
    worker_id = f"{s.worker_id}-{uuid.uuid4().hex[:6]}"
    if not s.openrouter_api_key:
        log.error("OPENROUTER_API_KEY is not set; every job would fail. Refusing to start.")
        return 2

    if s.require_provider_budget:
        try:
            billing.verify_provider_budget()
        except Exception as e:
            log.error("Provider budget verification failed (%s); configure a capped OpenRouter key", type(e).__name__)
            return 2
    Path(s.work_dir).mkdir(parents=True, exist_ok=True)
    db.bootstrap()
    log.info("model backend: %s; default model: %s", s.openrouter_url, s.default_model)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _shutdown.set())

    log.info("worker %s up: %s slot(s), work_dir=%s", worker_id, s.worker_concurrency, s.work_dir)
    threads = [threading.Thread(target=_janitor, daemon=True, name="janitor"),
               threading.Thread(target=_maintenance, daemon=True, name="outbox"),
               threading.Thread(target=_telemetry, args=(worker_id,), daemon=True, name="telemetry")]
    threads += [
        threading.Thread(target=_slot, args=(f"{worker_id}#{i}",), daemon=True, name=f"slot-{i}")
        for i in range(s.worker_concurrency)
    ]
    for t in threads:
        t.start()

    _shutdown.wait()
    log.info("worker %s draining; in-flight jobs get SIGTERM and are retried elsewhere", worker_id)
    deadline = time.monotonic() + 25
    for t in threads:
        t.join(timeout=max(0, deadline - time.monotonic()))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
