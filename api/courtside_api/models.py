"""Request/response models and the options -> CLI argv mapping.

Every option that reaches a subprocess argument is validated here. The worker
never builds a shell string -- argv is a list and `--` terminates option
parsing -- but a strict allowlist on top of that keeps a hostile `model` or
`from_ts` from being read as a flag by the child's argparse.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from .config import DEFAULT_OPENROUTER_MODEL, settings

# Must START alphanumeric: a leading "-" would be consumed as a flag by the
# child's argparse rather than read as the value of --server-model.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,119}$")
TS_RE = re.compile(r"^\d{1,3}(:\d{2}){0,2}(\.\d+)?$")

JobStatus = Literal["awaiting_upload", "queued", "running",
                    "succeeded", "failed", "cancelled", "expired"]


class JobOptions(BaseModel):
    """Analysis knobs, mapped 1:1 onto `courtside` CLI flags."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(
        default=None,
        description="Optional OpenRouter vision model ID. Omit to use the server's "
                    f"DEFAULT_MODEL ({DEFAULT_OPENROUTER_MODEL} out of the box). "
                    "Overrides must be in the operator's ALLOWED_MODELS. "
                    "The resolved model is returned in the queued job's options.model.",
        examples=[DEFAULT_OPENROUTER_MODEL],
    )
    max_clips: int = Field(
        default=0, ge=0, le=1000,
        description="Cap the clips analyzed. 0 = the whole video. Use 3 for a "
                    "limited preview; processing time and provider charges vary "
                    "with footage, model, and options.",
    )
    pose: bool = Field(default=False, description="Run pose overlays; requires an operator-enabled pose image.")
    heatmap: bool = Field(default=False, description="Render the court heatmap graphic.")
    moments: int = Field(default=6, ge=0, le=40, description="Flagged-moment deep dives to build.")
    fps: float = Field(default=4.0, gt=0, le=30)
    max_frames: int = Field(default=32, ge=4, le=128)
    max_side: int | None = Field(default=None, ge=224, le=2048)
    segment: Literal["auto", "fixed"] = "auto"
    window: float = Field(default=20.0, ge=2, le=120, description="Window seconds when segment=fixed.")
    from_ts: str | None = Field(default=None, description="Analyze from this timestamp, e.g. '12:30'.")
    to_ts: str | None = Field(default=None, description="Analyze up to this timestamp.")

    @field_validator("model")
    @classmethod
    def _check_model(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not MODEL_RE.match(v):
            raise ValueError("model must look like 'vendor/model-name'")
        return v

    @field_validator("from_ts", "to_ts")
    @classmethod
    def _check_ts(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not TS_RE.match(v):
            raise ValueError("timestamps look like 'SS', 'MM:SS' or 'HH:MM:SS'")
        return v

    def to_argv(self, video: str, out_dir: str, server_url: str, default_model: str) -> list[str]:
        """Render the CLI invocation. `video` and `out_dir` are server-controlled paths."""
        argv = [
            sys.executable, "-m", "courtside.analyze",
            "--out", out_dir,
            "--server-url", server_url,
            "--server-model", self.model or default_model,
            "--segment", self.segment,
            "--fps", str(self.fps),
            "--max-frames", str(self.max_frames),
            "--moments", str(self.moments),
        ]
        if self.segment == "fixed":
            argv += ["--window", str(self.window)]
        if self.max_clips:
            argv += ["--max-clips", str(self.max_clips)]
        if self.max_side:
            argv += ["--max-side", str(self.max_side)]
        if not self.pose:
            argv += ["--no-pose"]
        if self.heatmap:
            argv += ["--heatmap"]
        if self.from_ts:
            argv += ["--from-ts", self.from_ts]
        if self.to_ts:
            argv += ["--to-ts", self.to_ts]
        # "--" ends option parsing so a path can never be read as a flag.
        argv += ["--", video]
        return argv


# ------------------------------------------------------------------ requests

class CreateUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255, examples=["match.mp4"])
    content_type: str = Field(default="video/mp4", max_length=100)
    size_bytes: int | None = Field(
        default=None, ge=1,
        description="Required for a resumable multipart upload; omit for a single PUT.",
    )
    multipart: bool = Field(
        default=False,
        description="Resumable chunked upload. Strongly recommended on cellular.",
    )


class PartUrl(BaseModel):
    part_number: int
    url: str


class CreateUploadResponse(BaseModel):
    job_id: str
    status: JobStatus
    upload_url: str | None = None
    multipart_id: str | None = None
    part_size: int | None = None
    parts: list[PartUrl] | None = None
    expires_at: datetime
    max_upload_bytes: int


class CompletedPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=200)


class CompleteUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[CompletedPart] = Field(default_factory=list, max_length=10_000)

    @field_validator("parts")
    @classmethod
    def _unique_parts(cls, parts):
        numbers = sorted(p.part_number for p in parts)
        if numbers and numbers != list(range(1, len(parts) + 1)):
            raise ValueError("Include consecutive part numbers starting at 1, without duplicates")
        return parts


class UploadedPart(CompletedPart):
    size_bytes: int


class UploadedPartsResponse(BaseModel):
    job_id: str
    parts: list[UploadedPart]


class StartJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: JobOptions = Field(default_factory=JobOptions)
    webhook_url: str | None = Field(
        default=None, max_length=2000,
        description="POSTed on completion, signed with X-Courtside-Signature.",
    )

    @field_validator("webhook_url")
    @classmethod
    def _check_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        url = urlsplit(v)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.fragment or url.port not in (None, 443)):
            raise ValueError("webhook_url must use HTTPS on port 443 without credentials or a fragment")
        if not settings().webhook_secret:
            raise ValueError("Webhooks are disabled: WEBHOOK_SECRET is not configured")
        if url.hostname.lower() not in settings().webhook_allowed_hosts:
            raise ValueError("webhook_url host is not in WEBHOOK_ALLOWED_HOSTS")
        return v


# ----------------------------------------------------------------- responses

class JobReport(BaseModel):
    report_html_url: str | None = None
    session_json_url: str | None = None
    report_markdown_url: str | None = None
    court_anchor_url: str | None = None


class JobSummary(BaseModel):
    """The numbers a mobile client shows without downloading the full session."""

    video_duration_s: float | None = None
    clips_total: int | None = None
    clips_done: int | None = None
    cost_usd: float | None = None
    rallies: int | None = None
    strokes: int | None = None
    errors: int | None = None


class Job(BaseModel):
    id: str
    status: JobStatus
    phase: str
    progress: int
    filename: str | None = None
    video_bytes: int | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    summary: JobSummary = Field(default_factory=JobSummary)
    report: JobReport = Field(default_factory=JobReport)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    eta_s: int | None = Field(default=None, description="Rough seconds remaining, running jobs only.")


class JobList(BaseModel):
    jobs: list[Job]
    next_cursor: str | None = None


class JobExport(BaseModel):
    job: Job
    session: dict[str, Any] | None = None


class DeletionStatus(BaseModel):
    job_id: str
    state: Literal["not_requested", "pending", "deleted"]
    requested_at: datetime | None = None
    deleted_at: datetime | None = None
    upload_authorizations_expire_by: datetime | None = None


class JobUsage(BaseModel):
    confirmed_usd: float
    reserved_usd: float
    unsettled_requests: int
    requests: int


class LogResponse(BaseModel):
    job_id: str
    status: JobStatus
    phase: str
    progress: int
    lines: list[str]


class UsageResponse(BaseModel):
    account: str
    month: str
    spent_usd: float
    cap_usd: float
    remaining_usd: float
    jobs_run: int


class ErrorBody(BaseModel):
    detail: str
    code: str | None = None


# ------------------------------------------------------------------- admin

class CreateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=120)]
    email: str | None = Field(default=None, max_length=254)
    monthly_usd_cap: float = Field(default_factory=lambda: settings().default_monthly_usd_cap,
                                  ge=0, le=100_000)


class CreatedKey(BaseModel):
    account_id: str
    key_id: str
    name: str
    api_key: str = Field(description="Shown once. Store it now; only the hash is kept.")
    prefix: str
