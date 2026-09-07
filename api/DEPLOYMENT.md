# Deployment and operations

Deploy into accounts owned by the receiving team. Choose **Fly.io** for the
provided provisioning script, or **your own container infrastructure** below.
The API and worker share an image, Postgres database, private bucket, and secrets.
They are separate long-lived processes; a serverless HTTP function cannot run
the worker. This repository does not require the `cloud/` browser app.

## Required resources and configuration

| Resource | Requirements |
|---|---|
| Postgres | Version 14+; dedicated database; login with table/index creation permissions for initial bootstrap; TLS outside a private network |
| Object store | Private S3-compatible bucket; read/write/delete objects, list bucket, create/complete/abort multipart uploads; clients must reach the signed endpoint |
| Model service | Your OpenRouter key; defaults to `qwen/qwen3.8-27b`; verify with a short test |
| API | Start with 512 MiB–1 GiB RAM and 1 CPU; HTTPS at platform proxy/load balancer |
| Worker | Start with 2 CPUs, 8 GiB RAM, 100 GB writable scratch; concurrency 1; no GPU |
| Secrets | Random `ADMIN_TOKEN`; account API keys stored on your mobile backend; optional webhook secret and trusted callback hosts |

These are starting sizes, not capacity guarantees. Test your longest supported
video and concurrent uploads. Each job needs downloaded video, extracted frames,
model scratch, and base64 report space. Limit ingress/request rates at your edge.
An 8 GiB upload limit is checked at admission/start, not enforced as a hard byte
quota by a signed PUT; object lifecycle and storage quotas are also needed.

**The LLM runs through OpenRouter by default.** Supply your own
`OPENROUTER_API_KEY`; `DEFAULT_MODEL=qwen/qwen3.8-27b` and
`OPENROUTER_URL=https://openrouter.ai/api/v1` are already configured. Keep both
values unchanged for the standard setup. No GPU, MLX, local Qwen download, or
model server is needed. [Model rationale and data flow](README.md#default-llm).

All application settings are in [`.env.example`](.env.example). No application
code change is needed to use another Postgres host or S3-compatible provider.
`S3_*` values take precedence over `AWS_*`; `S3_BUCKET` falls back to Tigris's
`BUCKET_NAME`. AWS IAM roles can use boto3's default credential chain when static
keys are omitted. Set the actual AWS bucket region; `auto` is for Tigris/MinIO.
Temporary role credentials can expire before the requested signed-URL TTL.

## Fly.io deployment

Prerequisites: a Fly organization with billing, `flyctl`, Python 3, OpenSSL,
curl, an OpenRouter account, and your Postgres database. Install tools explicitly
in controlled CI; the script can install flyctl interactively when absent.
The script has been checked against local flyctl command help; provider-side
provisioning must still be verified in the receiving team's account.

Set `FLY_API_TOKEN`, `OPENROUTER_API_KEY`, and `DATABASE_URL` using your secret
manager or hidden shell input. Do not pass secret values as CLI flags. For Bash:

```bash
read -r -s -p 'Fly token: ' FLY_API_TOKEN; echo
read -r -s -p 'OpenRouter key: ' OPENROUTER_API_KEY; echo
read -r -s -p 'Postgres URL: ' DATABASE_URL; echo
export FLY_API_TOKEN OPENROUTER_API_KEY DATABASE_URL

# Uses no credentials/network and makes no changes:
./api/scripts/deploy.sh --app-name your-unique-app --org your-org --region ord --dry-run

# Creates billable resources and deploys to YOUR selected organization:
./api/scripts/deploy.sh --app-name your-unique-app --org your-org --region ord --yes
```

Use a token with permission to create apps, storage, and volumes in that org;
an app-only deploy token may not be sufficient for initial provisioning. The
script creates the app, Tigris bucket (default `<app-name>-media`), worker volume,
and generated admin/webhook secrets. It imports your database URL and model key,
builds from the repository root, deploys API/worker, and requires `/readyz` to
return 200. It does not configure your mobile backend, storage lifecycle, backups,
DNS custom domains, quotas, or alerts.

To bring an existing S3 bucket instead of provisioning Tigris, also export
`S3_BUCKET`, `S3_REGION`, credentials (or configure a runtime identity), and
`S3_ENDPOINT_URL` if required. Existing deployment secrets are respected. Ensure
old `S3_*` overrides are removed when changing to `AWS_*` credentials.

The script defaults to **your supplied/existing database**. An explicit
`--provision-postgres` opts into creating a single-node unmanaged Fly Postgres app
named `<app-name>-db` if needed. Fly distinguishes this from its managed Postgres
service; backup, upgrades, and availability of the unmanaged option remain your
responsibility. Prefer a managed service for a production handoff.
[Fly's database guidance](https://fly.io/docs/postgres/getting-started/what-you-should-know/).

| Flag/env | Default | Meaning |
|---|---|---|
| `--app-name` / `APP_NAME` | `courtside-api` | Pick a globally unique app name |
| `--org` / `FLY_ORG` | infer only if one org | Target receiving-team org |
| `--region` / `FLY_REGION` | `iad` | App/volume/provisioned DB region |
| `--bucket-name` / `BUCKET_NAME` | `<app-name>-media` | Name for new Tigris bucket |
| `--provision-postgres` | off | Explicit unmanaged Postgres opt-in |
| `--yes` | off | Fail instead of prompting for missing secrets |
| `--dry-run` | off | Offline plan; does not inspect existing resources |
| `--skip-flyctl-install` | off | Fail if flyctl is missing |
| `WORK_VOLUME_SIZE_GB` | 100 | New worker volume size |
| `PG_VM_SIZE`, `PG_VOLUME_SIZE_GB` | shared-cpu-1x, 10 | Unmanaged DB provisioning only |

Generated secrets are saved in `fly-<app-name>-credentials.txt` with mode 0600.
Move them to your secret manager and remove the file. Git/Docker ignore rules
exclude it; never distribute a raw workspace ZIP containing ignored files.
On re-run, the script preserves existing admin/webhook secrets, imports supplied
provider/database configuration, reuses resources, and deploys again. It cannot
recover lost secrets or verify a database merely from a secret's presence.
Changing names/regions can create additional billable resources.

`api/fly.toml` is a template. Use the script, which copies it temporarily to the
repo root with the selected app/region. This avoids flyctl resolving the
Dockerfile relative to `api/`. `--ha=false` starts a small initial fleet; inspect
`fly status -a your-app` and scale deliberately. At least one API machine remains
running in the primary region; workers are always-on. This is not a free tier.

Tigris provisioning sets `BUCKET_NAME` and `AWS_*` secrets. Keep the bucket private.
[Fly/Tigris configuration](https://fly.io/docs/tigris/).

## Your own container infrastructure

1. Provision Postgres and a private S3 bucket in your accounts.
2. Copy `api/.env.example` to `api/.env`, replace placeholders, and restrict access
   (`chmod 600 api/.env`). Do not use the local demo credentials in production.
3. From the repo root:

   ```bash
   docker compose --env-file api/.env -f api/compose.production.yml build api
   docker compose --env-file api/.env -f api/compose.production.yml up -d
   curl --fail http://localhost:8080/readyz
   ```

4. Point an HTTPS reverse proxy/load balancer at port 8080. The Compose example
   binds it to loopback; if the proxy is on another host/container, configure
   private routing explicitly. Expose neither Postgres nor the worker publicly.
5. Run the acceptance checks below through the public HTTPS address.

For ECS/Kubernetes/another platform, build `docker build -f api/Dockerfile .`,
push to your private registry, and use the same image for both commands:

```text
api:    uvicorn courtside_api.app:app --host 0.0.0.0 --port 8080 --workers 2
worker: python -m courtside_api.worker
```

Supply the environment to both, mount writable scratch at `/data` on each worker,
and allow at least 30 seconds for shutdown. The entrypoint initializes mounted
scratch directories then drops to UID 10001. If your platform runs non-root from
startup, provision `/data/work` and `/data/hf` with that UID's write permissions.
Workers do not serve HTTP: disable the image's HTTP healthcheck for them. Keep
worker scratch separate per replica. Do not scale a shared writable Compose
volume across hosts without defining the storage semantics.

Image dependency versions are constrained in `api/constraints.txt` for Linux /
Python 3.12; CPU Torch installs from PyTorch's CPU index. Base OS packages and
build tooling still receive updates on rebuild. For reproducible releases,
publish and deploy an immutable **image digest**, archive the dependency inventory,
and retain the previous digest for rollback. Review pins and licenses periodically.

## Updating an existing model configuration

New deployments use Qwen3.8 27B automatically. Existing explicit environment values
or Fly secrets can override the new defaults. Update an old `DEFAULT_MODEL` to
`qwen/qwen3.8-27b` in your secret manager / `api/.env`, then
recreate both API and worker processes. For the Fly script, export that value
before re-running the normal deployment command; it imports the explicit setting.
Do the same for `OPENROUTER_URL` if a previous deployment pointed at another server.

No database migration is required. Newly queued jobs use the new default; existing
jobs with a recorded model keep it. Older queued jobs with `options.model: null`
resolve the worker's current default when claimed. Verify the worker startup log
and a new job's `options.model`, then run the one-clip `--analyze` smoke test.

## Storage and retention

Before using real footage, configure lifecycle in your bucket provider:

- `uploads/`: expire source objects after your chosen short window (e.g. 2 days,
  greater than upload grace plus the maximum processing time).
- Abort incomplete multipart uploads after a short window (e.g. 1 day).
- `reports/`: choose and document retention with the product owner; include old
  object versions and backups in deletion requirements if versioning is enabled.

The worker attempts source deletion on its terminal results when
`DELETE_SOURCE_AFTER_ANALYSIS=true`. Expired reservations, pre-run cancellations,
failed cleanup calls, late PUTs to still-valid URLs, and hard worker crashes rely
on lifecycle. Expiry in Postgres alone does not remove objects. Crash leftovers
can also remain on scratch: clean directories for terminal jobs during maintenance,
never active job directories. Reports retain frames/clips and are sensitive even
after source deletion. OpenRouter and its chosen inference provider receive frames;
review their processing/retention terms for your intended use.

Native mobile uploads do not need CORS. For browser/hybrid uploads, configure the
bucket's CORS for your exact origins: PUT/GET/HEAD, required Content-Type headers,
and exposed `ETag`. API `CORS_ORIGINS` is separate and does not configure storage.

## Acceptance and operation

1. Confirm `/healthz` (process up) and `/readyz` (DB + bucket access) return 200.
   Readiness does not check worker capacity or provider credentials.
2. Confirm a worker is running and can write to scratch; inspect its logs.
3. Create an account using your stored admin secret; save the returned key and key ID
   on your backend. Set `COURTSIDE_API_KEY` locally for the smoke script.
4. Run `python3 api/scripts/smoke.py test.mp4 --base-url https://your-api`.
5. Run again with `--analyze` on a short, consented test MP4. This uses your credits.
   Check polling, summary, HTML/JSON/Markdown downloads, and source cleanup.
6. Exercise cancellation and an interrupted upload from a device. If using callbacks,
   configure trusted hosts/secret and verify HMAC + deduplication in your backend.

Monitor queued-job age, running-job heartbeat age, job failures/retries, worker
memory/disk, bucket growth, and provider spend. `/v1/usage` and `cost_usd` are
**estimated** from pipeline token statistics, not actual billing. Failed attempts
and in-flight spend can be missing. Admission checks are serialized per account,
but the monthly cap does not reserve spend or stop an ongoing job. Apply a real
provider budget and edge rate limits; add a spend ledger before selling usage.

Workers heartbeat every 15 seconds independently of stdout. A stale lease after
120 seconds is retried up to the job's `max_attempts` (default 2), and old attempts
cannot finalize newer ones. Retries restart analysis and can incur duplicate
provider charges. Cancellation and `JOB_TIMEOUT_S` interrupt silent subprocesses;
S3/network operations also have socket timeouts. Deploy/shutdown interrupts work
for later retry; it does not resume paid inference from a checkpoint.

Schema bootstrap is serialized and idempotent for **initial schema creation**.
It is not a versioned migration framework. Before future schema changes, add
explicit migrations, test an upgrade from the released schema, and take a backup.
Enable database backups/PITR in your provider and test restoration into a separate
database. Keep the release image digest and secret configuration alongside your
runbook, without putting secret values in source control.

For rollback, deploy the previous image digest with the same process/environment
configuration. Database changes must be backward-compatible or restored through
a planned recovery. For Fly, use `fly deploy --image <previous-image> -a <app>`
with a root-level copy of the correct app config. Scale workers only after adding
a suitable volume for each machine and checking provider limits.

Rotate account keys through admin endpoints. Rotate admin or webhook secrets
through your platform secret manager; webhook receivers must switch in coordination.
For teardown, inventory app machines/volumes, the database, bucket, registry images,
and backups in the receiving team's account; destroy only those selected resources.

## Local integration tests (no provider charges)

Use a **disposable** stack with no worker; tests create and delete their own accounts
and objects and exercise queue recovery. Never point these tests at production:

```bash
docker compose -p courtside-handoff -f api/docker-compose.yml up -d postgres minio createbucket
COURTSIDE_INTEGRATION=1 \
DATABASE_URL=postgres://courtside:courtside@localhost:5432/courtside \
ADMIN_TOKEN=integration-admin S3_BUCKET=courtside \
S3_ENDPOINT_URL=http://localhost:9000 S3_PUBLIC_ENDPOINT_URL=http://localhost:9000 \
S3_ACCESS_KEY_ID=courtside S3_SECRET_ACCESS_KEY=courtside123 S3_FORCE_PATH_STYLE=true \
.venv/bin/python -m pytest api/integration -q
docker compose -p courtside-handoff -f api/docker-compose.yml down -v
```

Install the dependencies from the API README first. Unit tests deliberately mock
DB/storage; these integration tests use real services. Neither replaces the paid
provider acceptance test in the receiving team's environment.
