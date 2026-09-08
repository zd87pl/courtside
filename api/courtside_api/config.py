"""Environment-driven settings.

Deliberately stdlib + dataclass rather than pydantic-settings: the API image
already carries FastAPI, boto3 and psycopg, and one more dependency for
`os.environ.get` is not worth it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "qwen/qwen3.8-27b"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = (os.environ.get(name) or "").strip() or default
    if required and not val:
        raise RuntimeError(f"{name} is required but not set")
    return val or ""


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- core ---
    database_url: str
    admin_token: str
    openrouter_api_key: str
    openrouter_url: str = DEFAULT_OPENROUTER_URL
    default_model: str = DEFAULT_OPENROUTER_MODEL

    # --- object storage (Tigris on Fly, MinIO locally, any S3) ---
    s3_bucket: str = "courtside"
    s3_endpoint: str = ""
    s3_public_endpoint: str = ""  # URL reachable by upload/download clients
    s3_region: str = "auto"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_force_path_style: bool = False

    # --- limits ---
    max_upload_bytes: int = 8 * 1024**3        # 8 GiB
    upload_url_ttl_s: int = 6 * 3600           # long enough for a cellular upload
    download_url_ttl_s: int = 3600
    upload_grace_s: int = 24 * 3600            # unclaimed uploads expire
    job_timeout_s: int = 4 * 3600              # hard ceiling on one analysis
    default_monthly_usd_cap: float = 50.0
    max_concurrent_jobs_per_account: int = 3
    max_pending_uploads: int = 20
    uploads_per_hour: int = 30
    starts_per_hour: int = 30
    require_user_id: bool = True
    allowed_models: tuple[str, ...] = field(default_factory=tuple)
    enable_pose: bool = False
    request_reserve_usd: float = 0.50
    require_provider_budget: bool = True
    report_retention_days: int = 0

    # --- worker ---
    worker_id: str = ""
    worker_concurrency: int = 1
    work_dir: str = "/data/work"
    poll_interval_s: float = 3.0
    # Best-effort source deletion after terminal worker results. Bucket lifecycle
    # is still required for abandoned uploads; reports also contain footage.
    delete_source_after_analysis: bool = True
    heartbeat_s: float = 15.0
    stale_after_s: int = 120                   # no heartbeat -> reclaim the job

    # --- webhooks ---
    webhook_secret: str = ""
    webhook_allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    webhook_timeout_s: float = 10.0
    webhook_max_attempts: int = 5

    cors_origins: tuple[str, ...] = field(default_factory=tuple)

    @property
    def s3_configured(self) -> bool:
        return bool(self.s3_access_key and self.s3_secret_key and self.s3_bucket)


@lru_cache(maxsize=1)
def settings() -> Settings:
    origins = tuple(o.strip() for o in _env("CORS_ORIGINS").split(",") if o.strip())
    return Settings(
        database_url=_env("DATABASE_URL", required=True),
        admin_token=_env("ADMIN_TOKEN", required=True),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        openrouter_url=_env("OPENROUTER_URL", DEFAULT_OPENROUTER_URL),
        default_model=_env("DEFAULT_MODEL", DEFAULT_OPENROUTER_MODEL),
        s3_bucket=_env("S3_BUCKET", _env("BUCKET_NAME", "courtside")),
        s3_endpoint=_env("S3_ENDPOINT_URL", _env("AWS_ENDPOINT_URL_S3")),
        s3_public_endpoint=_env("S3_PUBLIC_ENDPOINT_URL"),
        s3_region=_env("S3_REGION", _env("AWS_REGION", "auto")),
        s3_access_key=_env("S3_ACCESS_KEY_ID", _env("AWS_ACCESS_KEY_ID")),
        s3_secret_key=_env("S3_SECRET_ACCESS_KEY", _env("AWS_SECRET_ACCESS_KEY")),
        s3_force_path_style=_bool("S3_FORCE_PATH_STYLE"),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 8 * 1024**3),
        upload_url_ttl_s=_int("UPLOAD_URL_TTL_S", 6 * 3600),
        download_url_ttl_s=_int("DOWNLOAD_URL_TTL_S", 3600),
        upload_grace_s=_int("UPLOAD_GRACE_S", 24 * 3600),
        job_timeout_s=_int("JOB_TIMEOUT_S", 4 * 3600),
        default_monthly_usd_cap=_float("DEFAULT_MONTHLY_USD_CAP", 50.0),
        max_concurrent_jobs_per_account=_int("MAX_CONCURRENT_JOBS_PER_ACCOUNT", 3),
        max_pending_uploads=max(1, _int("MAX_PENDING_UPLOADS", 20)),
        uploads_per_hour=max(1, _int("UPLOADS_PER_HOUR", 30)),
        starts_per_hour=max(1, _int("STARTS_PER_HOUR", 30)),
        require_user_id=_bool("REQUIRE_USER_ID", True),
        allowed_models=tuple(m.strip() for m in _env("ALLOWED_MODELS").split(",") if m.strip()),
        enable_pose=_bool("ENABLE_POSE", False),
        request_reserve_usd=max(0.01, _float("REQUEST_RESERVE_USD", 0.50)),
        require_provider_budget=_bool("REQUIRE_PROVIDER_BUDGET", True),
        report_retention_days=max(0, _int("REPORT_RETENTION_DAYS", 0)),
        worker_id=_env("WORKER_ID", _env("FLY_MACHINE_ID", "worker-local")),
        worker_concurrency=max(1, _int("WORKER_CONCURRENCY", 1)),
        work_dir=_env("WORK_DIR", "/data/work"),
        poll_interval_s=_float("POLL_INTERVAL_S", 3.0),
        delete_source_after_analysis=_bool("DELETE_SOURCE_AFTER_ANALYSIS", True),
        heartbeat_s=_float("HEARTBEAT_S", 15.0),
        stale_after_s=_int("STALE_AFTER_S", 120),
        webhook_secret=_env("WEBHOOK_SECRET"),
        webhook_allowed_hosts=tuple(h.strip().lower() for h in
                                    _env("WEBHOOK_ALLOWED_HOSTS").split(",") if h.strip()),
        webhook_timeout_s=_float("WEBHOOK_TIMEOUT_S", 10.0),
        webhook_max_attempts=_int("WEBHOOK_MAX_ATTEMPTS", 5),
        cors_origins=origins,
    )
