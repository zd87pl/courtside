"""FastAPI surface for the Courtside job API.

Shape note, because it drives everything else: a full match is 100+ rallies and
runs for roughly an hour on real footage, and the source file is often over a
gigabyte. So there is no synchronous "POST a video, get a report" endpoint --
there cannot be. The flow is:

    POST /v1/uploads              -> job id + presigned URL(s)
    PUT  <presigned url>          -> phone uploads straight to object storage
    POST /v1/jobs/{id}/start      -> queued
    GET  /v1/jobs/{id}            -> poll phase/progress, or take the webhook
    GET  /v1/jobs/{id}/report     -> session facts + signed artifact URLs

Interactive docs live at /docs.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import __version__, auth, db, jobs, storage
from .config import settings
from .models import (CompleteUploadRequest, CreateAccountRequest, CreatedKey,
                     CreateUploadRequest, CreateUploadResponse, Job, JobList,
                     JobReport, JobSummary, LogResponse, PartUrl,
                     StartJobRequest, UsageResponse)

log = logging.getLogger("courtside.api")

ARTIFACT_NAMES = {"report.html", "session.json", "session_report.md", "court_anchor.jpg"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.bootstrap()
    log.info("courtside-api %s ready", __version__)
    yield
    db.close()


app = FastAPI(
    title="Courtside API",
    version=__version__,
    description=__doc__,
    lifespan=lifespan,
)

if settings().cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings().cors_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )


# --------------------------------------------------------------- serializing

def _eta_s(row: dict) -> int | None:
    """Crude but honest: elapsed / fraction-done, minus elapsed.

    Progress is not linear in time (analysis dominates), so this is a hint for a
    progress label, not a promise. Below 8% there is nothing to extrapolate from.
    """
    if row["status"] != "running" or not row.get("started_at") or row["progress"] < 8:
        return None
    elapsed = (datetime.now(timezone.utc) - row["started_at"]).total_seconds()
    total = elapsed / (row["progress"] / 100.0)
    return max(0, int(total - elapsed))


def _report_urls(row: dict) -> JobReport:
    prefix = row.get("report_prefix")
    if row["status"] != "succeeded" or not prefix:
        return JobReport()
    ttl = settings().download_url_ttl_s
    artifacts = (row.get("session_json") or {}).get("_artifacts", [])
    return JobReport(
        report_html_url=storage.presign_get(f"{prefix}/report.html", ttl),
        session_json_url=storage.presign_get(f"{prefix}/session.json", ttl),
        report_markdown_url=storage.presign_get(f"{prefix}/session_report.md", ttl),
        court_anchor_url=(storage.presign_get(f"{prefix}/court_anchor.jpg", ttl)
                          if "court_anchor.jpg" in artifacts else None),
    )


def _summary(row: dict) -> JobSummary:
    doc = row.get("session_json") or {}
    facts = doc.get("facts") or {}
    return JobSummary(
        video_duration_s=row.get("video_duration_s"),
        clips_total=row.get("clips_total"),
        clips_done=row.get("clips_done"),
        cost_usd=float(row["cost_usd"]) if row.get("cost_usd") is not None else None,
        rallies=facts.get("clips_analyzed", facts.get("rallies")),
        strokes=facts.get("total_strokes", facts.get("strokes")),
        errors=(facts.get("outcomes") or {}).get("errors", facts.get("errors")),
    )


def _job(row: dict, *, urls: bool = True) -> Job:
    return Job(
        id=str(row["id"]),
        status=row["status"],
        phase=row["phase"],
        progress=row["progress"],
        filename=row.get("filename"),
        video_bytes=row.get("video_bytes"),
        options=row.get("options") or {},
        error=row.get("error"),
        error_code=row.get("error_code"),
        summary=_summary(row),
        report=_report_urls(row) if urls else JobReport(),
        created_at=row["created_at"],
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        eta_s=_eta_s(row),
    )


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job.") from None


def _load(job_id: str, principal: auth.Principal) -> dict:
    row = jobs.get(_uuid(job_id), principal.account_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job.")
    return row


# -------------------------------------------------------------------- health

@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "version": __version__}


@app.get("/readyz", include_in_schema=False)
def readyz(response: Response) -> dict:
    checks = {"database": db.ping(), "storage": storage.ping()}
    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ok": ok, "checks": checks, "version": __version__}


# ------------------------------------------------------------------- uploads

@app.post("/v1/uploads", response_model=CreateUploadResponse, status_code=201,
          tags=["uploads"], summary="Reserve a job and get a presigned upload URL")
def create_upload(body: CreateUploadRequest,
                  principal: auth.Principal = auth.AccountDep) -> CreateUploadResponse:
    """Step 1. The video is PUT straight to object storage, not through this API.

    Pass `multipart: true` with `size_bytes` for a resumable chunked upload --
    on cellular that is the difference between retrying one 32 MiB part and
    re-sending the whole match.
    """
    s = settings()
    if body.size_bytes and body.size_bytes > s.max_upload_bytes:
        raise HTTPException(413,
                            f"Video exceeds the {s.max_upload_bytes} byte limit. "
                            "Downscale to 720p before uploading; the pipeline samples "
                            "frames at ~784px and gains nothing from a larger source.")
    if body.multipart and not body.size_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "size_bytes is required when multipart is true.")
    if not body.multipart and body.size_bytes and body.size_bytes > 5 * 1024**3:
        raise HTTPException(400, "Use multipart for files larger than 5 GiB.")

    row = jobs.create(principal.account_id, filename=body.filename,
                      content_type=body.content_type, video_key_fn=storage.video_key)
    key = row["video_key"]
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=s.upload_url_ttl_s)

    if body.multipart:
        plan = storage.plan_multipart(key, body.size_bytes, body.content_type)
        with db.conn() as c:
            c.execute("UPDATE jobs SET multipart_id = %s, video_bytes = %s WHERE id = %s",
                      (plan.upload_id, body.size_bytes, row["id"]))
        return CreateUploadResponse(
            job_id=str(row["id"]), status=row["status"], multipart_id=plan.upload_id,
            part_size=plan.part_size,
            parts=[PartUrl(**p) for p in plan.urls],
            expires_at=expires_at, max_upload_bytes=s.max_upload_bytes,
        )

    return CreateUploadResponse(
        job_id=str(row["id"]), status=row["status"],
        upload_url=storage.presign_put(key, body.content_type),
        expires_at=expires_at, max_upload_bytes=s.max_upload_bytes,
    )


@app.post("/v1/uploads/{job_id}/complete", response_model=Job, tags=["uploads"],
          summary="Finish a multipart upload")
def complete_upload(job_id: str, body: CompleteUploadRequest,
                    principal: auth.Principal = auth.AccountDep) -> Job:
    """Step 1b, multipart only. `parts` carries the ETag each PUT returned."""
    row = _load(job_id, principal)
    if row["status"] != "awaiting_upload":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This job is already {row['status']}.")
    if not row["multipart_id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This job was created for a single PUT, not a multipart upload.")
    if not body.parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No parts supplied.")

    try:
        storage.complete_multipart(row["video_key"], row["multipart_id"],
                                   [p.model_dump() for p in body.parts])
    except storage.StorageError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    meta = storage.head(row["video_key"])
    return _job(jobs.mark_uploaded(_uuid(job_id), principal.account_id,
                                   meta["size"] if meta else None) or row)


# ---------------------------------------------------------------------- jobs

@app.post("/v1/jobs/{job_id}/start", response_model=Job, tags=["jobs"],
          summary="Queue the analysis")
def start_job(job_id: str, body: StartJobRequest,
              principal: auth.Principal = auth.AccountDep) -> Job:
    """Step 2. Returns immediately; the analysis runs on a worker.

    Omit options.model to use the configured OpenRouter Qwen3.8 27B default.
    The selected model is saved in options.model before queuing, so a deployment
    change cannot switch an already queued job to a different default model.
    Use options.max_clips=3 for a preview; latency and provider charges vary.
    """
    row = _load(job_id, principal)
    if row["status"] == "queued":
        return _job(row)  # retrying start must not replace options or fail a cap check
    if row["status"] in jobs.TERMINAL or row["status"] == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This job is already {row['status']}.")

    meta = storage.head(row["video_key"])
    if meta is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "No video at this job's upload location yet. Complete the "
                            "upload before starting the analysis.")
    if meta["size"] > settings().max_upload_bytes:
        raise HTTPException(413, "Uploaded video is too large.")

    spent, _ = jobs.month_usage(principal.account_id)
    if spent >= principal.monthly_usd_cap:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"Monthly spend cap reached (${spent:.2f} of ${principal.monthly_usd_cap:.2f}).")
    if jobs.running_count(principal.account_id) >= settings().max_concurrent_jobs_per_account:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many analyses in flight for this account. Wait for one to finish.")

    jobs.mark_uploaded(_uuid(job_id), principal.account_id, meta["size"])
    options = body.options.model_dump(mode="json")
    options["model"] = body.options.model or settings().default_model
    try:
        row = jobs.enqueue(_uuid(job_id), principal.account_id,
                           options, body.webhook_url)
    except jobs.AdmissionError as e:
        raise HTTPException(e.status_code, str(e)) from e
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This job can no longer be started.")
    return _job(row)


@app.get("/v1/jobs/{job_id}", response_model=Job, tags=["jobs"],
         summary="Poll a job")
def get_job(job_id: str, principal: auth.Principal = auth.AccountDep) -> Job:
    """Step 3. Poll every few seconds, or supply `webhook_url` and skip polling."""
    return _job(_load(job_id, principal))


@app.get("/v1/jobs", response_model=JobList, tags=["jobs"], summary="List jobs")
def list_jobs(principal: auth.Principal = auth.AccountDep,
              limit: int = Query(default=25, ge=1, le=100),
              before: datetime | None = None,
              job_status: str | None = Query(default=None, alias="status")) -> JobList:
    rows = jobs.list_for_account(principal.account_id, limit=limit,
                                 before=before, status=job_status)
    # No presigned URLs in a list: signing four URLs per row would turn a
    # 100-row page into 400 signatures for links nobody clicked.
    return JobList(
        jobs=[_job(r, urls=False) for r in rows],
        next_cursor=rows[-1]["created_at"].isoformat() if len(rows) == limit else None,
    )


@app.get("/v1/jobs/{job_id}/report", tags=["jobs"], summary="Fetch the finished report")
def get_report(job_id: str, principal: auth.Principal = auth.AccountDep) -> dict:
    """Step 4. Session facts inline, plus signed URLs for the artifacts.

    `report.html` is fully self-contained (CSS, keyframes and clips are embedded)
    so it renders in a WKWebView / WebView straight from the signed URL.
    """
    row = _load(job_id, principal)
    if row["status"] != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"No report: this job is {row['status']}.")
    return {
        "job_id": str(row["id"]),
        "summary": _summary(row).model_dump(),
        "report": _report_urls(row).model_dump(),
        "session": row.get("session_json") or {},
    }


@app.get("/v1/jobs/{job_id}/report/{artifact}", tags=["jobs"],
         summary="Redirect to one signed artifact", response_class=RedirectResponse)
def get_artifact(job_id: str, artifact: str,
                 principal: auth.Principal = auth.AccountDep) -> RedirectResponse:
    """A stable URL a WebView can load directly; 307s to a short-lived signed URL."""
    if artifact not in ARTIFACT_NAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Unknown artifact. Available: {sorted(ARTIFACT_NAMES)}")
    row = _load(job_id, principal)
    if row["status"] != "succeeded" or not row.get("report_prefix"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"No report: this job is {row['status']}.")
    return RedirectResponse(
        storage.presign_get(f"{row['report_prefix']}/{artifact}"),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@app.get("/v1/jobs/{job_id}/logs", response_model=LogResponse, tags=["jobs"],
         summary="Tail the pipeline log")
def get_logs(job_id: str, principal: auth.Principal = auth.AccountDep,
             lines: int = Query(default=100, ge=1, le=400)) -> LogResponse:
    row = _load(job_id, principal)
    tail = (row.get("log_tail") or "").splitlines()[-lines:]
    return LogResponse(job_id=str(row["id"]), status=row["status"], phase=row["phase"],
                       progress=row["progress"], lines=tail)


@app.post("/v1/jobs/{job_id}/cancel", response_model=Job, tags=["jobs"],
          summary="Cancel a queued or running job")
def cancel_job(job_id: str, principal: auth.Principal = auth.AccountDep) -> Job:
    """Queued jobs stop at once; a running job is killed at its next heartbeat."""
    row = jobs.request_cancel(_uuid(job_id), principal.account_id)
    if row is None:
        existing = _load(job_id, principal)
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"This job is already {existing['status']}.")
    return _job(row)


@app.get("/v1/usage", response_model=UsageResponse, tags=["account"],
         summary="Month-to-date spend against the cap")
def usage(principal: auth.Principal = auth.AccountDep) -> UsageResponse:
    spent, n = jobs.month_usage(principal.account_id)
    return UsageResponse(
        account=principal.account_name,
        month=datetime.now(timezone.utc).strftime("%Y-%m"),
        spent_usd=round(spent, 4),
        cap_usd=principal.monthly_usd_cap,
        remaining_usd=round(max(0.0, principal.monthly_usd_cap - spent), 4),
        jobs_run=n,
    )


# --------------------------------------------------------------------- admin

@app.post("/v1/admin/accounts", response_model=CreatedKey, status_code=201,
          tags=["admin"], dependencies=[auth.AdminDep],
          summary="Create an account and its first API key")
def create_account(body: CreateAccountRequest) -> CreatedKey:
    key, prefix, key_hash = auth.generate_key()
    account_id, key_id = uuid.uuid4(), uuid.uuid4()
    with db.conn() as c, c.transaction():
        c.execute(
            "INSERT INTO accounts (id, name, email, monthly_usd_cap) VALUES (%s, %s, %s, %s)",
            (account_id, body.name, body.email, body.monthly_usd_cap),
        )
        c.execute(
            "INSERT INTO api_keys (id, account_id, prefix, key_hash) VALUES (%s, %s, %s, %s)",
            (key_id, account_id, prefix, key_hash),
        )
    return CreatedKey(account_id=str(account_id), key_id=str(key_id), name=body.name,
                      api_key=key, prefix=prefix)


@app.post("/v1/admin/accounts/{account_id}/keys", response_model=CreatedKey, status_code=201,
          tags=["admin"], dependencies=[auth.AdminDep], summary="Mint another API key")
def create_key(account_id: str, name: str = Query(default="default", max_length=120)) -> CreatedKey:
    acct_uuid = _uuid(account_id)
    key, prefix, key_hash = auth.generate_key()
    key_id = uuid.uuid4()
    with db.conn() as c:
        acct = c.execute("SELECT name FROM accounts WHERE id = %s", (acct_uuid,)).fetchone()
        if acct is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")
        c.execute(
            "INSERT INTO api_keys (id, account_id, name, prefix, key_hash) VALUES (%s, %s, %s, %s, %s)",
            (key_id, acct_uuid, name, prefix, key_hash),
        )
    return CreatedKey(account_id=account_id, key_id=str(key_id), name=acct["name"], api_key=key, prefix=prefix)


@app.delete("/v1/admin/keys/{key_id}", status_code=204, tags=["admin"],
            dependencies=[auth.AdminDep], summary="Revoke an API key")
def revoke_key(key_id: str) -> Response:
    with db.conn() as c:
        row = c.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
            (_uuid(key_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such active key.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
