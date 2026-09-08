"""Small authenticated operations surface; never includes keys or footage."""
from . import db
from .config import settings


def snapshot():
    with db.conn() as c:
        jobs = c.execute("""SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
            count(*) FILTER (WHERE status = 'running') AS running,
            count(*) FILTER (WHERE status = 'running' AND heartbeat_at < now() - (%s * interval '1 second')) AS stale_running,
            extract(epoch FROM now() - min(queued_at) FILTER (WHERE status = 'queued')) AS oldest_queued_s,
            count(*) FILTER (WHERE deletion_requested_at IS NOT NULL AND deleted_at IS NULL) AS pending_deletions
            FROM jobs""", (settings().stale_after_s,)).fetchone()
        workers = c.execute("""SELECT count(*) AS healthy_workers, COALESCE(sum(slots), 0) AS total_slots
            FROM workers WHERE heartbeat_at > now() - interval '60 seconds'""").fetchone()
        outbox = c.execute("""SELECT count(*) FILTER (WHERE delivered_at IS NULL AND failed_at IS NULL) AS pending,
            count(*) FILTER (WHERE failed_at IS NOT NULL) AS failed FROM webhook_outbox""").fetchone()
        billing = c.execute("""SELECT count(*) FILTER (WHERE actual_usd IS NULL) AS unsettled_requests,
            COALESCE(sum(reserved_usd) FILTER (WHERE actual_usd IS NULL), 0) AS reserved_usd FROM model_usage""").fetchone()
        migrations = c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return {'jobs': jobs, 'workers': workers, 'webhooks': outbox, 'billing': billing,
            'migrations': [m['version'] for m in migrations]}
