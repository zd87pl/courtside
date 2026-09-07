-- Bootstrapped on startup; every statement is idempotent so there are no
-- migrations to run for the initial deploy.

-- gen_random_uuid() is built in on supported PostgreSQL 14+.

CREATE TABLE IF NOT EXISTS accounts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text NOT NULL,
    email            text,
    monthly_usd_cap  numeric(10, 2) NOT NULL DEFAULT 50,
    created_at       timestamptz NOT NULL DEFAULT now(),
    disabled_at      timestamptz
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name          text NOT NULL DEFAULT 'default',
    prefix        text NOT NULL,
    key_hash      text NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    revoked_at    timestamptz
);
CREATE INDEX IF NOT EXISTS api_keys_account_idx ON api_keys (account_id);

-- status: awaiting_upload | queued | running | succeeded | failed | cancelled | expired
CREATE TABLE IF NOT EXISTS jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status            text NOT NULL DEFAULT 'awaiting_upload',
    phase             text NOT NULL DEFAULT 'queued',
    progress          int NOT NULL DEFAULT 0,
    filename          text,
    content_type      text,
    video_key         text NOT NULL,
    video_bytes       bigint,
    multipart_id      text,
    options           jsonb NOT NULL DEFAULT '{}'::jsonb,
    webhook_url       text,
    idempotency_key   text,
    error             text,
    error_code        text,
    cost_usd          numeric(10, 4),
    video_duration_s  real,
    clips_total       int,
    clips_done        int,
    report_prefix     text,
    session_json      jsonb,
    log_tail          text,
    attempts          int NOT NULL DEFAULT 0,
    max_attempts      int NOT NULL DEFAULT 2,
    worker_id         text,
    cancel_requested  boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    queued_at         timestamptz,
    started_at        timestamptz,
    finished_at       timestamptz,
    heartbeat_at      timestamptz,
    expires_at        timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_account_idx ON jobs (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs (status, queued_at)
    WHERE status IN ('queued', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency_idx ON jobs (account_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    url           text NOT NULL,
    event         text NOT NULL,
    attempts      int NOT NULL DEFAULT 0,
    status_code   int,
    error         text,
    delivered_at  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS webhook_job_idx ON webhook_deliveries (job_id);
