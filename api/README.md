# Courtside mobile API

A phone uploads a tennis video to your private object store; a worker analyzes it
with the shared Python pipeline and OpenRouter; the app receives structured facts
and a self-contained HTML report. No GPU is required on the API or worker host.

**New here? Follow the [repository quick start](../README.md#local-api-quick-start).**
Use [Deployment](DEPLOYMENT.md) for your infrastructure and
[Mobile integration](MOBILE_INTEGRATION.md) for the client contract.
Read [Operations](OPERATIONS.md) for the 0.2.0 upgrade, quotas, usage ledger,
recovery, erasure, and backups. The Next.js `cloud/` app is independent: it does not share API accounts or jobs.

## Default LLM

| Setting | Default / action |
|---|---|
| Model provider | OpenRouter, `https://openrouter.ai/api/v1` |
| Vision-language model | **Qwen3.8 27B** |
| Exact OpenRouter ID | `qwen/qwen3.8-27b` |
| Model credential | Set your own `OPENROUTER_API_KEY` with available credits/model access and a finite spending limit |
| Client model selection | Omit `options.model`; the service chooses and records the default |
| GPU or locally hosted LLM | Not required; CPU video tooling is included, pose is an optional build |

Qwen3.8 27B is a dense vision-language model with image and video understanding.
OpenRouter lists image/video input and JSON-schema structured-output support,
which fit this pipeline's sampled-frame analysis and structured results.
[Qwen model card](https://huggingface.co/Qwen/Qwen3.8-27B),
[OpenRouter model listing](https://openrouter.ai/qwen/qwen3.8-27b).

Qwen3.8 enables thinking by default. For this model, Courtside sends
`reasoning: {"effort": "none"}` to reserve the bounded output budget for results.
This setting is retained on schema-fallback retries; a provider that rejects it
returns an error instead of silently re-enabling thinking.
[OpenRouter reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

“Best” on a specific tennis dataset still needs evaluation: this repository has
not established a tennis benchmark winner. Use the included smoke test to verify
your key/provider route, and review results on representative footage before
setting quality expectations. Provider prices, availability, and latency can change.

The worker segments the uploaded video and sends sampled JPEG frames plus timing
information to OpenRouter's chat-completions API; it does not send a raw video as
a model request. The same selected model also generates the report and optional
coaching cards. A missing OpenRouter key stops the worker with a clear error;
there is no silent fallback to a local LLM or a different model.

For a normal deployment, leave these defaults as supplied:

```dotenv
DEFAULT_MODEL=qwen/qwen3.8-27b
OPENROUTER_URL=https://openrouter.ai/api/v1
```

Overrides are optional: an operator can set `DEFAULT_MODEL`, and your backend can
supply `options.model` for one job when included in `ALLOWED_MODELS` (empty permits
only the default). Precedence is **job model → server DEFAULT_MODEL
→ built-in Qwen3.8 27B default**. Use a model that accepts images. The API saves the
resolved model when a job is queued, so a later default change does not alter that
job. It is visible in the start/poll response's `options.model` and in the finished
session's model metadata. Worker startup logs show the default endpoint/model.
Local Compose forwards exported model settings; Fly's deploy script imports them
when explicitly supplied. See deployment instructions for existing installations.

## Local quick start

From the repository root, with Docker Compose v2 installed. Use Bash for these
commands and leave the service terminal running:

```bash
# Required: credits, default-model access, and a finite OpenRouter key budget.
# Bash example (input hidden); alternatively use your secret manager.
read -r -s -p 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
export OPENROUTER_API_KEY
docker compose -f api/docker-compose.yml up --build
```

In a second terminal, wait for `curl --fail http://localhost:8080/readyz` to return
200. API docs: http://localhost:8080/docs. MinIO console: http://localhost:9001
(`courtside` / `courtside123`, local demo only). This stack uses persistent named
volumes and binds ports to loopback. `down` preserves data; `down -v` deletes it.

Create a test account (the response contains its API key once):

```bash
curl --fail-with-body http://localhost:8080/v1/admin/accounts \
  -H 'X-Admin-Token: local-admin-token' -H 'Content-Type: application/json' \
  -d '{"name":"Test Club"}'
```

Save `account_id` and `key_id` for administration/revocation. Set the returned
`api_key` in the second Bash terminal (it is different from the OpenRouter key):

```bash
read -r -s -p 'Courtside account API key: ' COURTSIDE_API_KEY; echo
export COURTSIDE_API_KEY
```

Then, from the repository root, use Python 3 to exercise the deployment with a
short MP4 you have permission to process. Replace `test.mp4` with its actual path:

```bash
python3 api/scripts/smoke.py test.mp4             # upload/complete/cancel; no model calls
python3 api/scripts/smoke.py test.mp4 --analyze   # one clip; uses your provider credits
```

The smoke script uses only Python's standard library. The analysis check disables
pose/moments and verifies HTML, JSON, and Markdown downloads. It should end with
`Analysis and artifact downloads passed.` For a deployed service, add
`--base-url https://your-api.example` and use an account key from that deployment.

The first image build downloads ffmpeg and Python dependencies. Pose is disabled
and absent by default; [enable it explicitly](OPERATIONS.md#optional-pose-image)
after reviewing its terms. A worker without an OpenRouter key or verifiable finite
key budget exits; the API still supports upload tests. The local stack does not automatically read `api/.env`.

For physical-device development, expose the API and object endpoint through your
own development network/tunnel and set `S3_PUBLIC_ENDPOINT_URL` to that reachable
storage URL. `localhost` refers to the phone when used on a phone; the default
loopback port bindings intentionally need adjustment before LAN access works.

## Contract

```text
Mobile app → your authenticated backend → POST /v1/uploads
Mobile app → PUT signed URLs directly to your private S3 bucket
Your backend → POST /v1/uploads/{id}/complete  (multipart only)
Your backend → POST /v1/jobs/{id}/start
Your backend → GET /v1/jobs/{id} → report URLs when succeeded
```

Keep account API keys on your backend. Send `X-Courtside-User-Id` from the verified
app session on every account-authenticated request. The API scopes jobs to that
account/user; the header does not replace your login system. The smoke script sends
`smoke-test-user` by default (`COURTSIDE_USER_ID` overrides it).
Use `/docs` or `/openapi.json` for request models and the integration guide for
retry, polling, WebView, and webhook behavior.

## Validation

From the repository root, with Python 3.11+ and ffmpeg installed:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,server]' -r api/requirements.txt httpx
python -m pytest tests api/tests -q
```

Unit/regression tests cover request validation, worker interruption and cleanup,
progress, signatures, storage signing, and auth. The separate integration suite
uses real Postgres and S3; instructions are in the deployment guide. Only a paid
smoke run checks your provider key, chosen model, and actual footage end to end.
