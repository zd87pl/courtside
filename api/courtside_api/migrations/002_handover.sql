ALTER TABLE jobs ADD COLUMN IF NOT EXISTS owner_id text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS request_hash text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS requested_bytes bigint;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS upload_multipart boolean NOT NULL DEFAULT false;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deletion_requested_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_cleaned_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS checkpoint_key text;
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(account_id, owner_id, created_at DESC);
CREATE TABLE IF NOT EXISTS rate_limits (
    scope text NOT NULL,
    bucket bigint NOT NULL,
    used int NOT NULL,
    PRIMARY KEY(scope, bucket)
);
CREATE TABLE IF NOT EXISTS model_usage (
    id uuid PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt int NOT NULL,
    model text NOT NULL,
    state text NOT NULL DEFAULT 'reserved',
    reserved_usd numeric(14, 6) NOT NULL,
    actual_usd numeric(14, 8),
    provider_id text,
    prompt_tokens bigint,
    completion_tokens bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS usage_account_month_idx ON model_usage(account_id, created_at);
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event text NOT NULL,
    url text NOT NULL,
    payload jsonb NOT NULL,
    attempts int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    delivered_at timestamptz,
    failed_at timestamptz,
    status_code int,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id, event)
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON webhook_outbox(next_attempt_at)
    WHERE delivered_at IS NULL AND failed_at IS NULL;
CREATE TABLE IF NOT EXISTS deletion_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    action text NOT NULL,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workers (
    id text PRIMARY KEY,
    slots int NOT NULL,
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);

-- Preserve pre-ledger estimates, explicitly labelled rather than calling them actual provider costs.
INSERT INTO model_usage(id, account_id, job_id, attempt, model, state, reserved_usd, created_at, finished_at)
SELECT gen_random_uuid(), account_id, id, attempts, COALESCE(options->>'model', 'legacy'),
    'legacy_estimate', cost_usd, COALESCE(finished_at, created_at), finished_at
FROM jobs WHERE cost_usd IS NOT NULL;
