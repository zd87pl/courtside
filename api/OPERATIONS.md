# Handover safeguards and operations

These controls are part of API version **0.2.0**. They apply to the Python API and
worker. The handover also includes the independent `cloud/` app as a private
prototype; use its [own setup and acceptance guide](../cloud/README.md).

## Upgrade from the original handoff

1. Back up Postgres and retain the previous image digest. The service applies
   checksummed, ordered migrations under a database lock at startup. `001` adopts
   the original `schema.sql`; `002` adds ownership, usage, outbox, and deletion data;
   `003` rotates cleanup batches so failed/pending items cannot starve later jobs.
   Applied SQL files are immutable. Add a new numbered file for each future change.
2. Update your backend to send `X-Courtside-User-Id` on every account-authenticated
   request. Derive this opaque, stable ID from your verified login/session; never
   forward a user-selected identity from the phone. The account API key still stays
   on your backend. Admin endpoints use only the admin token.
3. Existing jobs have `owner_id = NULL` and are hidden from scoped users until your
   operator backfills them from the backend's authoritative user/job mapping.
   `REQUIRE_USER_ID=false` permits a temporary trusted-backend compatibility mode;
   leaving the header out in that mode gives account-wide access. Prefer completing
   the mapping before enabling traffic. Deploy matching API/worker versions together.
4. Give the service an OpenRouter API key with a finite spending limit and available
   credits. Worker startup verifies the key's limit. `REQUIRE_PROVIDER_BUDGET=false`
   is an explicit escape hatch for local test stubs/other endpoints, not a substitute
   for a production budget. The deployment never creates or changes your provider key.
5. Review the controls in `.env.example`, configure object permissions/lifecycle,
   and run the smoke/integration checks. Pose is off and absent from the default
   image. Existing jobs explicitly requesting pose need a pose-capable deployment.

[OpenRouter key-budget API](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key).

## Admission and model costs

`MAX_PENDING_UPLOADS` (20), `UPLOADS_PER_HOUR` (30), and `STARTS_PER_HOUR` (30)
are shared per account across API replicas. A new user ID does not bypass them.
Identical upload-idempotency retries and queued-start retries do not consume a new
reservation/start allowance. Limits use fixed UTC-hour buckets; retain ingress
rate/request-size limits at the edge for unauthenticated traffic.

`ALLOWED_MODELS` is a comma-separated allowlist. Empty means only `DEFAULT_MODEL`.
Both the API and the per-request worker hook enforce it. The default remains
`qwen/qwen3.8-27b` through OpenRouter.

Before **each** model request, including schema fallback, repairs, report generation,
and retry attempts, the worker reserves `REQUEST_RESERVE_USD` (default $0.50)
in Postgres under an account lock. It refuses the next call if confirmed spend
plus unresolved reservations plus this hold would exceed the account's monthly cap.
SDK automatic retries are disabled in metered worker runs so requests are recorded
individually. Lease loss, cancellation, or deletion prevents a new reservation.

Successful responses settle with OpenRouter's `usage.cost`. A timeout, ambiguous
server error, or worker crash retains its reservation. Known request rejections
release the hold; generations with an ID but no cost are periodically reconciled
through the provider's generation endpoint. Legacy pre-ledger estimates remain
labelled `legacy_estimate`; they are not relabelled as confirmed provider costs.
[Provider usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

`GET /v1/jobs/{id}/usage` separates `confirmed_usd`, `reserved_usd`, and
`unsettled_requests`. `/v1/usage` and the job's `cost_usd` include unresolved holds.
The reservation is an operator-selected allowance, not a guaranteed upper bound
on a provider's charge; a request can cost more. Keep a provider-side budget,
review the allowance for your chosen model/input limits, and reconcile records
before using them for customer invoices. Calls with no recoverable generation ID
need operator reconciliation against provider records; they never silently become
zero-cost. Payment collection and customer billing policy belong to your product.

## Upload and analysis recovery

Pass an `Idempotency-Key` (8–200 letters/digits or `._:-`) when reserving an upload.
The key is scoped to an account and tied to the normalized request and user. Reuse
returns the same job and multipart upload ID; a different request/user returns 409.
A key for a started/expired/deleted reservation also returns 409; poll the original
job or use a new key for a genuinely new upload.

`POST /v1/uploads/{id}/refresh` reissues URLs within the original 24-hour reservation
window. `GET /v1/uploads/{id}/parts` lists stored part numbers, ETags, and sizes.
Neither extends the reservation indefinitely. Single-PUT upload URLs can also be
refreshed. Keep the original plan and part-size/chunk boundaries on the client.

The worker persists completed clip JSON/stats to `checkpoints/` and fences the
checkpoint pointer by worker/attempt. On another worker it re-downloads the source,
extracts frames, and reuses clips only when the source ETag, options, and pipeline
code hash match. Reports and moment passes can run again. Calls interrupted before
a checkpoint was persisted may repeat; all attempts remain in the usage ledger.

## Webhook outbox

Terminal job rows feed a durable outbox, including pre-run cancellation and expired
uploads that have a configured callback. Events are leased with `SKIP LOCKED`, signed
again on each attempt, and retried after worker restart. Receivers must deduplicate
by the stable `delivery_id`; a lost response can cause duplicate delivery. Retain
polling for app recovery and when callbacks fail.

After the configured retry budget (default five), an item remains failed for
inspection. Query `webhook_outbox` using restricted operator DB access; fix the
receiver/configuration, then `POST /v1/admin/webhooks/{delivery_id}/retry` with the
admin token. This preserves the delivery ID. Delivered items cannot be replayed
through that endpoint. Deleting a job cancels its stored pending callbacks; a
request already in flight may still reach the receiver.

## Deletion and retention

`DELETE /v1/jobs/{id}` checks the user/account, immediately hides the job and report
URLs from normal API routes, requests cancellation, and returns 202. Poll
`GET /v1/deletions/{id}` with the same identity. The maintenance worker removes
sources, checkpoints, report objects, **all object versions/delete markers**, and
incomplete multipart uploads. It scrubs filename, session content, logs, errors,
and callback data, while retaining opaque ownership/job IDs and accounting/audit
records. `GET /v1/jobs/{id}/export` provides an authorized metadata/artifact export
before deletion.

Old signed PUTs cannot be revoked individually. Cleanup repeats until the original
upload window plus a five-minute clock margin has elapsed; only then does deletion
report `deleted`. Failed S3 operations remain pending with entries in
`deletion_audit`. Readiness alone does not prove deletion permissions are correct.
For a strict erasure workflow, wait for deletion completion and check the audit.

Storage credentials need list/delete object versions, bucket versioning lookup,
list multipart uploads, and abort multipart uploads, in addition to normal
read/write access. Object locks or missing permissions can prevent deletion.
Keep lifecycle rules as a fallback for hard crashes and untracked incomplete
uploads. Each worker also removes its own terminal-job scratch directories.

`REPORT_RETENTION_DAYS=0` leaves reports until explicit deletion. Choose a positive
number for automatic terminal-job expiry; enabling it applies to existing jobs too.
Make that policy decision explicitly before public use. Source cleanup follows
`DELETE_SOURCE_AFTER_ANALYSIS`, which remains enabled by default.

Live-store erasure does not erase historical backups or downstream provider copies.
Choose backup expiry, provider processing terms, and consent notices with the product
owner. A restore must replay the latest deletion manifest before serving traffic.

## Monitoring and backup restoration

`GET /v1/admin/operations` (admin token required) exposes queue depth/oldest age,
stale running leases, recent worker heartbeats/slots, pending deletions, failed
webhooks, unsettled costs, and applied migrations. Alert on a growing queue with
no healthy workers, stale leases, persistent deletion failures, and unsettled
reservations. `GET /v1/admin/usage/unsettled` lists records needing reconciliation.
Worker telemetry runs separately from callbacks and storage maintenance.

Use your managed database's backups/PITR and periodically restore into an isolated
environment. The supplied CLI supports a repeatable local/own-host check. Install
the API dependencies plus compatible `pg_dump`/`pg_restore` tools. With an existing
local Postgres container, `PG_CONTAINER=<container-name>` uses its client tools
through `docker exec`; connection values stay in the environment.

From the repo root, with `DATABASE_URL` set through your secret manager:

```bash
mkdir -p backups
python api/scripts/database.py backup backups/release.dump

# Export again from the CURRENT database immediately before a restore;
# retain deletion manifests independently of old backups.
python api/scripts/database.py export-deletions backups/current-deletions.json

# Set RESTORE_DATABASE_URL to an EMPTY, separate test database first.
python api/scripts/database.py restore backups/release.dump \
  --deletions backups/current-deletions.json
```

The restore command refuses a populated target, restores transactionally, applies
current migrations (including for a legacy backup), and replays newer deletion
tombstones/scrubbing. Keep that database disconnected from
public traffic and production storage until migrations, erasure replay, row checks,
and a disposable-bucket smoke test pass. If erasure replay fails, discard the
isolated restore and retry; do not bring it online. Backups/manifests use restrictive
file modes and ignored paths, but still need encryption, access controls, and expiry
in your backup system. Avoid including real database URLs on command lines.

## Optional pose image

The default build omits Torch, Ultralytics, and YOLO weights. For a deployment whose
operator has reviewed the applicable terms, set both `INSTALL_POSE=true` (build)
and `ENABLE_POSE=true` (runtime) in `api/.env`, then build and start the
own-infrastructure Compose stack from the deployment guide. Requests must explicitly
set `options.pose=true`. With the local Compose stack, export `INSTALL_POSE=true`
before rebuilding both services. The Fly provisioning script builds the default
image; a pose deployment needs an explicitly built/published pose image.
[Licensing notes](../docs/third-party.md).
