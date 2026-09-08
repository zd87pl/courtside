"""FastAPI surface for the Courtside job API.

Video analysis runs asynchronously on a worker. Processing time depends on the
footage, model, and options; upload bytes go directly to object storage. The flow is:

    POST /v1/uploads              -> job id + presigned URL(s)
    PUT  <presigned url>          -> phone uploads straight to object storage
    POST /v1/uploads/{id}/complete -> finish multipart uploads only
    POST /v1/jobs/{id}/start      -> queued
    GET  /v1/jobs/{id}            -> poll phase/progress, or take the webhook
    GET  /v1/jobs/{id}/report     -> session facts + signed artifact URLs

Interactive docs live at /docs.
"""

from __future__ import annotations

import logging
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Query, Response, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import __version__, auth, db, jobs, storage
from .config import settings
from .models import (CompleteUploadRequest, CreateAccountRequest, CreatedKey,
                     CreateUploadRequest, CreateUploadResponse, Job, JobList,
                     JobReport, JobSummary, LogResponse, PartUrl,
                     StartJobRequest, UsageResponse, UploadedPartsResponse,
                     DeletionStatus, JobExport, JobUsage)

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

@app.exception_handler(jobs.AdmissionError)
async def admission_error(request: Request, exc: jobs.AdmissionError):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


if settings().cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings().cors_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Courtside-User-Id"],
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
    if row is None or (principal.owner_id is not None and row.get("owner_id") != principal.owner_id):
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
                  idempotency_key: str | None = Header(default=None, min_length=8, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
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
                      content_type=body.content_type, video_key_fn=storage.video_key,
                      owner_id=principal.owner_id, idempotency_key=idempotency_key,
                      request_hash=hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest(),
                      requested_bytes=body.size_bytes, upload_multipart=body.multipart)
    return _upload_plan(row)


def _upload_plan(row: dict) -> CreateUploadResponse:
    ttl = min(settings().upload_url_ttl_s,
              int((row["expires_at"] - datetime.now(timezone.utc)).total_seconds()))
    if ttl <= 0 or row["status"] != "awaiting_upload":
        raise HTTPException(409, "Upload reservation expired or already started.")
    data = dict(job_id=str(row["id"]), status=row["status"],
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
                max_upload_bytes=settings().max_upload_bytes)
    if not row.get("upload_multipart"):
        return CreateUploadResponse(**data, upload_url=storage.presign_put(
            row["video_key"], row["content_type"], expires=ttl))
    # Serialize multipart initialization/refresh so a retry reuses the upload ID.
    with db.conn() as c, c.transaction():
        current = c.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (row["id"],)).fetchone()
        if current["status"] != "awaiting_upload" or current["deletion_requested_at"]:
            raise HTTPException(409, "Reservation is no longer uploadable.")
        ttl = min(settings().upload_url_ttl_s, int((current["expires_at"] - datetime.now(timezone.utc)).total_seconds()))
        if ttl <= 0:
            raise HTTPException(409, "Upload reservation expired.")
        if current["multipart_id"]:
            plan = storage.resume_multipart(current["video_key"], current["multipart_id"],
                                             current["requested_bytes"], ttl)
        else:
            plan = storage.plan_multipart(current["video_key"], current["requested_bytes"],
                                          current["content_type"], expires=ttl)
            c.execute("UPDATE jobs SET multipart_id = %s WHERE id = %s", (plan.upload_id, row["id"]))
    return CreateUploadResponse(**data, multipart_id=plan.upload_id, part_size=plan.part_size,
                                parts=[PartUrl(**p) for p in plan.urls])


@app.post("/v1/uploads/{job_id}/refresh", response_model=CreateUploadResponse, tags=["uploads"])
def refresh_upload(job_id: str, principal: auth.Principal = auth.AccountDep):
    """Reissue signed URLs within the original reservation's lifetime."""
    return _upload_plan(_load(job_id, principal))


@app.get("/v1/uploads/{job_id}/parts", response_model=UploadedPartsResponse, tags=["uploads"])
def uploaded_parts(job_id: str, principal: auth.Principal = auth.AccountDep):
    """List completed multipart parts and ETags for interrupted device uploads."""
    row = _load(job_id, principal)
    if row["status"] != "awaiting_upload" or not row["multipart_id"]:
        raise HTTPException(409, "No active multipart upload.")
    return {"job_id": job_id, "parts": storage.list_parts(row["video_key"], row["multipart_id"])}


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
            f"Monthly accounted spend/reservation cap reached (${spent:.2f} of ${principal.monthly_usd_cap:.2f}).")
    if jobs.running_count(principal.account_id) >= settings().max_concurrent_jobs_per_account:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many analyses in flight for this account. Wait for one to finish.")

    jobs.mark_uploaded(_uuid(job_id), principal.account_id, meta["size"])
    options = body.options.model_dump(mode="json")
    options["model"] = body.options.model or settings().default_model
    allowed = settings().allowed_models or (settings().default_model,)
    if options["model"] not in allowed:
        raise HTTPException(422, "Model is not in the operator's ALLOWED_MODELS.")
    if (options["pose"] or options["heatmap"]) and not settings().enable_pose:
        raise HTTPException(422, "Pose is disabled; the operator must enable a pose-capable image.")
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
    """Step 3. Poll every few seconds and back off in the background.

    Optional signed webhooks use a durable outbox; polling is still needed for recovery.
    """
    return _job(_load(job_id, principal))


@app.get("/v1/jobs", response_model=JobList, tags=["jobs"], summary="List jobs")
def list_jobs(principal: auth.Principal = auth.AccountDep,
              limit: int = Query(default=25, ge=1, le=100),
              before: datetime | None = None,
              job_status: str | None = Query(default=None, alias="status")) -> JobList:
    rows = jobs.list_for_account(principal.account_id, limit=limit,
                                 before=before, status=job_status, owner_id=principal.owner_id)
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
    _load(job_id, principal)
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


@app.delete("/v1/jobs/{job_id}", status_code=202, response_model=DeletionStatus, tags=["privacy"])
def delete_job(job_id: str, principal: auth.Principal = auth.AccountDep):
    """Hide results immediately and queue live-store erasure, including object versions."""
    from .privacy import request_delete
    return request_delete(_uuid(job_id), principal)


@app.get("/v1/deletions/{job_id}", response_model=DeletionStatus, tags=["privacy"])
def deletion_status(job_id: str, principal: auth.Principal = auth.AccountDep):
    from .privacy import deletion_status as status_for
    return status_for(_uuid(job_id), principal)


@app.get("/v1/jobs/{job_id}/export", response_model=JobExport, tags=["privacy"])
def export_job(job_id: str, principal: auth.Principal = auth.AccountDep):
    """Owner-authorized metadata export; signed URLs link to complete result artifacts."""
    row = _load(job_id, principal)
    return {"job": _job(row), "session": row.get("session_json")}


@app.get("/v1/jobs/{job_id}/usage", response_model=JobUsage, tags=["account"])
def job_usage(job_id: str, principal: auth.Principal = auth.AccountDep):
    from .billing import summary
    row = _load(job_id, principal)
    return summary(principal.account_id, row["id"])


@app.get("/v1/admin/operations", dependencies=[auth.AdminDep], tags=["admin"])
def operations():
    """Monitor queue age, stale leases, worker capacity, outbox failures and unresolved costs."""
    from .operations import snapshot
    return snapshot()


@app.post("/v1/admin/webhooks/{delivery_id}/retry", dependencies=[auth.AdminDep], tags=["admin"])
def retry_webhook(delivery_id: str):
    """Retry a failed outbox item after fixing the receiver; preserves its delivery ID."""
    with db.conn() as c:
        row = c.execute("""UPDATE webhook_outbox SET attempts = 0, failed_at = NULL,
            error = NULL, next_attempt_at = now(), lease_until = NULL, lease_token = NULL
            WHERE id = %s AND failed_at IS NOT NULL AND delivered_at IS NULL RETURNING id""",
            (_uuid(delivery_id),)).fetchone()
    if not row:
        raise HTTPException(409, "Delivery is not in the failed outbox.")
    return {"delivery_id": str(row["id"]), "status": "pending"}


@app.get("/v1/admin/usage/unsettled", dependencies=[auth.AdminDep], tags=["admin"])
def unsettled_usage(limit: int = Query(default=100, ge=1, le=1000)):
    """Inspect retained reservations; reconcile unknown costs with provider records."""
    with db.conn() as c:
        return c.execute("""SELECT id, account_id, job_id, attempt, model, state,
            reserved_usd, provider_id, created_at FROM model_usage WHERE actual_usd IS NULL
            ORDER BY created_at LIMIT %s""", (limit,)).fetchall()
