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

Courtside authenticates **accounts**, not mobile users. Store `cs_live_...` keys
in your backend's secret manager. Never compile them, `ADMIN_TOKEN`, storage
credentials, or the OpenRouter key into an app. Your backend must persist
`app_user_id → job_id → courtside_account_id` and authorize each request before
proxying it. One key can read all jobs in its account. Sign-up/login, app sessions,
push notifications, billing, and a mobile SDK are not implemented here.

API calls use `Authorization: Bearer <account-key>`. Admin routes instead require
`X-Admin-Token`. Creating an account or an additional key returns `account_id`,
`key_id`, `api_key`, `name`, and `prefix`; plaintext keys are returned once. Rotate
by minting a new key, updating your backend, then calling
`DELETE /v1/admin/keys/{key_id}`. Admin-token rotation does not revoke account keys.

## Upload and start

1. Your backend calls `POST /v1/uploads`:

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
   Your backend may supply an image-capable `options.model` as an explicit override.

   Returns immediately with `queued`. `max_clips: 0` means the whole match;
   pose and moments default on. Enable them after the minimal integration works.
   Every analyzed clip may incur provider charges. See `/docs` for all options.

For small uploads, omit `multipart` (or set false); PUT the file to `upload_url`
with the exact `Content-Type` declared when reserving it, then call start.
Single PUTs are limited to 5 GiB by S3; use multipart above that size. Upload URL
expiry defaults to six hours. There is currently no URL refresh or part-listing
endpoint: retain the plan and ETags locally, and reserve a new job after expiry.

`POST /v1/uploads` is **not idempotent**: a lost response can leave an abandoned
reservation. The service expires these after 24 hours; bucket lifecycle handles
their objects and incomplete parts. `Idempotency-Key` is not implemented. Start
retries while queued return the original options; retries after running/terminal
return 409, so poll the existing job instead of creating another.

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
account jobs newest first; URL-encode the timestamp cursor. List responses omit
signed artifact URLs. `/v1/jobs/{id}/logs` is a support endpoint, not UI copy.

## Errors and retries

| HTTP | Meaning |
|---|---|
| 400 | Invalid upload plan/completion, such as missing multipart size or using single PUT above 5 GiB |
| 401 | Missing, invalid, or revoked account key |
| 403 | Disabled account or invalid admin token |
| 404 | Unknown job, or job belonging to another account |
| 409 | Upload missing or state transition not allowed; poll the job |
| 402 | Estimated monthly spend cap reached |
| 413 | Source too large |
| 422 | Request validation failed (including an unapproved webhook host) |
| 429 | Account concurrency limit; retry after a job finishes |
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

The worker POSTs a `job.completed` event with `job_id`, `status`, `phase`,
`progress`, `error_code`, optional cost/counts, and `finished_at`.
Verify before parsing/acting:

```text
X-Courtside-Signature: t=<unix-seconds>,v1=<hex>
expected = HMAC-SHA256(WEBHOOK_SECRET, ASCII(t) + "." + raw_request_body)
```

Use constant-time comparison and reject timestamps outside ±300 seconds.
`courtside_api.webhooks.verify` is a Python reference. Deduplicate by
`job_id + status`, return 2xx promptly, then poll the authoritative job and send
your push notification. The secret is deployment-wide and should be shared only
with your trusted backend, not with individual customers.

Webhooks are best effort with up to five in-process attempts. They are not a
durable event queue; worker death, upload expiry, or pre-run cancellation may
produce no callback. Polling remains necessary for recovery.
