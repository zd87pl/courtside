ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cleanup_attempted_at timestamptz;
CREATE INDEX IF NOT EXISTS jobs_cleanup_pending_idx ON jobs(cleanup_attempted_at NULLS FIRST)
    WHERE status IN ('succeeded', 'failed', 'cancelled', 'expired') AND deleted_at IS NULL;
