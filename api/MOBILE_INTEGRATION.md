# Mobile integration contract

Use [openapi.json](openapi.json) for offline client generation, or fetch
`/openapi.json` from your deployed version. Regenerate after API changes with
`python api/scripts/export_openapi.py > api/openapi.json` from the repository root
(with the API dependencies installed).

## Ownership and authentication

Use this topology:

```text
Phone ── user login/token ──> Your backend ── account API key ──> Courtside API
Phone ── short-lived signed PUT/GET ──────────────────────────> Object storage
Courtside worker ── optional signed callback ──> Your backend ── APNs/FCM ──> Phone
```

Courtside authenticates your backend with an **account key** and scopes jobs to
a delegated user identity. Store `cs_live_...` keys in your backend secret manager.
Never compile these, `ADMIN_TOKEN`, storage credentials, or the OpenRouter key into
an app. Derive `X-Courtside-User-Id` from your verified app login/session and send
it on every account-authenticated request. Never forward a caller-selected ID.
The header is required by default (1–200 characters); it does not verify a JWT
or perform login. A compromised account key can impersonate users in its account.
Persist `app_user_id → job_id → courtside_account_id` and authorize requests before
proxying. Sign-up/login, push notifications, billing, and a mobile SDK belong to
your product. Existing deployments must follow the [ownership upgrade](OPERATIONS.md#upgrade-from-the-original-handoff).

API calls use `Authorization: Bearer <account-key>` and
`X-Courtside-User-Id: <verified-app-user-id>`. Admin routes instead require
`X-Admin-Token`. Creating an account or an additional key returns `account_id`,
`key_id`, `api_key`, `name`, and `prefix`; plaintext keys are returned once. Rotate
by minting a new key, updating your backend, then calling
`DELETE /v1/admin/keys/{key_id}`. Admin-token rotation does not revoke account keys.

## Upload and start

1. Your backend calls `POST /v1/uploads`, preferably with an `Idempotency-Key`
   generated once per intended upload (a UUID works). Store it before the call:

   ```json
   {"filename":"match.mp4","content_type":"video/mp4","size_bytes":70000000,"multipart":true}
   ```

   Persist `job_id` immediately. The response supplies `multipart_id`, `part_size`,
   `parts: [{part_number,url}]`, `expires_at`, and `max_upload_bytes`. The default
   limit is 8 GiB. Prefer exporting a short 720p/1080p MP4 for an initial test.
2. The phone reads contiguous chunks of `part_size` bytes and PUTs each to its
   corresponding URL; the final chunk can be smaller. Send the raw bytes, not
   `multipart/form-data`. Save the response `ETag` including its quotes and its
   part number. Limit parallel transfers (e.g. 2–3) and retry failed parts with
   exponential backoff before expiry. **Do not add the Courtside Authorization
   header to storage requests or change the signed host/query string.**
3. Your backend calls `POST /v1/uploads/{job_id}/complete`:

   ```json
   {"parts":[{"part_number":1,"etag":"\"etag-from-storage\""}]}
   ```

   Include every part exactly once. Retrying a completed upload is supported
   while its object exists. Completion leaves the job `awaiting_upload`; it does
   not queue analysis.
4. Your backend calls `POST /v1/jobs/{job_id}/start`:

   ```json
   {"options":{"max_clips":3,"pose":false,"moments":0}}
   ```

   The example omits `options.model` intentionally: the default is
   `qwen/qwen3.8-27b` through OpenRouter. Your phone/backend needs
   no model-provider credentials in the request; the service operator configures
   `OPENROUTER_API_KEY`. The response's `options.model` records the selected model.
   Your backend may select an image-capable override only from the operator's
   `ALLOWED_MODELS`; by default only `DEFAULT_MODEL` is accepted.

   Returns immediately with `queued`. `max_clips: 0` means the whole match;
   pose defaults off and moments default to six. Pose/heatmaps require an explicitly
   enabled pose image; use the limited preview before enabling additional work.
   Every analyzed clip may incur provider charges. See `/docs` for all options.

For small uploads, omit `multipart` (or set false); PUT the file to `upload_url`
with the exact `Content-Type` declared when reserving it, then call start.
Single PUTs are limited to 5 GiB by S3; use multipart above that size. Upload URL
expiry defaults to six hours. `POST /v1/uploads/{id}/refresh` returns fresh URLs
within the original 24-hour reservation window, and `GET /v1/uploads/{id}/parts`
returns stored part numbers, ETags, and sizes. Keep the original chunk boundaries.
After the reservation expires, start a new upload with a new key.

An `Idempotency-Key` must be 8–200 letters/digits or `._:-`. Identical retries
return the same reservation and multipart ID. Reusing it with another body/user,
or after the reservation starts/expires/is deleted, returns 409. Without a key,
a lost response can leave an abandoned reservation. Maintenance and bucket
lifecycle handle abandoned data. Queued start retries return the original options;
retries after running/terminal return 409, so poll the existing job.

## Polling, results and cancellation

Poll `GET /v1/jobs/{job_id}` every 3–5 seconds in the foreground; back off in the
background and resume on app launch. Treat these statuses as follows:

| Status | Client action |
|---|---|
| `awaiting_upload` | Finish upload, then request start |
| `queued` | Waiting for worker capacity |
| `running` | Show phase and progress; `eta_s` is a rough estimate or null |
| `succeeded` | Fetch `/v1/jobs/{id}/report` |
| `failed` | Display a retry option; record job ID and `error_code` for support |
| `cancelled` | Terminal; a new analysis needs a new reservation |
| `expired` | Reservation expired; upload again with a new reservation |

`progress` is 0–100 and may reset on a worker retry. Reports can be partial if
some clips fail. `summary.clips_done` counts successful clips; inspect session
facts and clip statuses rather than assuming every detected rally succeeded.
`rallies` counts analyzed clips, `strokes` are sampled estimates, and `errors`
reflect model judgments. These are not authoritative match statistics.

`GET /v1/jobs/{id}/report` returns `{job_id, summary, report, session}`. `report`
contains signed `report_html_url`, `session_json_url`, `report_markdown_url`, and
optional `court_anchor_url` (null when unavailable). `session` is a reduced
server copy; use `session_json_url` for the full pipeline document. API v1 has a
stable HTTP envelope; the internal session schema may gain fields.

Load the HTML signed URL directly in WKWebView/Android WebView without account
credentials. It embeds CSS, images, and short clips and can be large. Disable
unneeded JavaScript/native bridges and restrict external navigation. Signed URLs
expire after one hour by default; fetch the report endpoint again for fresh URLs.
They are bearer credentials: anyone holding a URL can use it until expiry.
The authenticated `/report/report.html` route returns a 307 to the same storage
URL; do not forward an account Authorization header across that redirect.

`POST /v1/jobs/{id}/cancel` cancels queued/reserved jobs immediately and requests
interruption of running work. Poll until terminal. Already-paid inference cannot
be refunded by cancellation. `GET /v1/jobs?limit=25&before=<next_cursor>` lists
the authenticated account/user's jobs newest first; URL-encode the timestamp cursor. List responses omit
signed artifact URLs. `/v1/jobs/{id}/logs` is a support endpoint, not UI copy.

## Usage, export, and deletion

`GET /v1/jobs/{id}/usage` reports confirmed provider cost separately from unresolved
reservations; `cost_usd` includes both. It is not an invoice. For data access, call
`GET /v1/jobs/{id}/export` to obtain job/session metadata and available signed
artifacts, and download artifacts before their URLs expire.

`DELETE /v1/jobs/{id}` returns 202, hides the job immediately, requests cancellation,
and schedules content erasure. Poll `GET /v1/deletions/{id}` with the same user ID.
Erasure includes live objects/versions, multipart uploads, checkpoints, and stored
content; opaque ownership/accounting/audit records remain. Old signed PUTs can
survive temporarily, so cleanup remains pending until the original upload window
plus a five-minute margin ends. Backup/provider retention is an operator policy;
see [deletion operations](OPERATIONS.md#deletion-and-retention).

## Errors and retries

| HTTP | Meaning |
|---|---|
| 400 | Missing/invalid user identity or invalid upload plan/completion, such as missing multipart size or using single PUT above 5 GiB |
| 401 | Missing, invalid, or revoked account key |
| 403 | Disabled account or invalid admin token |
| 404 | Unknown/deleted job, or job belonging to another account/user |
| 409 | Upload missing, idempotency conflict, or state transition not allowed; poll the job |
| 402 | Monthly spend plus unresolved reservations reached the account cap |
| 413 | Source too large |
| 422 | Request validation failed (including an unapproved webhook host) |
| 429 | Account concurrency/pending-upload limit or hourly upload/start allowance; wait for capacity/window reset |
| 5xx | Service/dependency error; retry reads with bounded backoff |

FastAPI errors use `detail`, which may be a string or validation-error array.
Do not display raw backend logs to app users. Network uncertainty on a mutating
request should be resolved by checking the known job first.

## Optional webhooks

Configure `WEBHOOK_SECRET` and `WEBHOOK_ALLOWED_HOSTS=api.your-company.example`
on the service, then pass `webhook_url` to start. Hosts are an exact allowlist
(no wildcards), HTTPS port 443 only; credentials, fragments, redirects, and
ambient HTTP proxies are rejected/disabled. Only allow domains controlled by
your backend team, not tenant-controlled hosts. Apply network egress controls
if your environment requires protection against compromised DNS.

The worker POSTs a `job.completed` event with stable `delivery_id`, `job_id`, `status`, `phase`,
`progress`, `error_code`, optional cost/counts, and `finished_at`.
Verify before parsing/acting:

```text
X-Courtside-Signature: t=<unix-seconds>,v1=<hex>
expected = HMAC-SHA256(WEBHOOK_SECRET, ASCII(t) + "." + raw_request_body)
```

Use constant-time comparison and reject timestamps outside ±300 seconds.
`courtside_api.webhooks.verify` is a Python reference. Deduplicate by
`delivery_id`, return 2xx promptly, then poll the authoritative job and send
your push notification. The secret is deployment-wide and should be shared only
with your trusted backend, not with individual customers.

A durable Postgres outbox retries delivery across worker restarts, up to five
attempts by default. Lost responses can produce duplicates. Exhausted or permanent
failures remain recorded; an operator can replay failed events after fixing the
receiver. Deletion removes pending callbacks, though an in-flight request can
still arrive. Polling remains necessary for app recovery.
