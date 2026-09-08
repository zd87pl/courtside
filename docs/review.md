# Handoff code review — 2026-09-07

Scope: API/worker lifecycle, tenant boundaries, object storage, deployment scripts,
shared pipeline output, browser routes/dependencies, and documented setup. Existing
uncommitted work was preserved and extended; no live cloud resources were deployed.

## Findings fixed

| Priority | Finding | Change and verification |
|---|---|---|
| P1 | Worker heartbeat, cancellation and timeout depended on subprocess stdout; quiet inference could be reclaimed while still running | Independent heartbeat monitor across transfer/inference/publication; interruptible stdout reader; silent-child regression tests |
| P1 | An old worker could update/finalize a job after another attempt claimed it | Worker ID + attempt fencing; separate attempt artifact/scratch paths; real Postgres recovery test |
| P1 | A source-deletion failure overwrote a successful report with failure | Cleanup runs after finalization; regression test preserves success |
| P1 | Local signed URLs used Docker's internal `minio` hostname | Separate public signing endpoint; real signed PUT and multipart tests |
| P1 | Concurrent starts bypassed the account job limit; queued retries changed options | Per-account transaction lock and immutable queued retries; concurrent Postgres test |
| P1 | Caller-controlled webhook destinations allowed arbitrary outbound HTTPS and redirects | Operator-controlled exact host allowlist, required signing, no redirects/proxies, malformed-target tests |
| P1 | Concurrent first boots could race schema creation | Transaction-scoped advisory lock around bootstrap; real service startup |
| P1 | Fresh mounted volumes could hide image-time permissions | Entrypoint initializes scratch then drops privileges; container runtime check |
| P1 | Browser session creation accepted arbitrary player/team IDs | Enforce team membership/coaching authority and self-only player sessions |
| P1 | Browser dependency audit reported vulnerabilities | Compatible Next.js/transitive updates and PostCSS override; lockfile, audit, tests/build |
| P2 | API read `rallies/strokes/errors` fields that the pipeline does not emit | Map `clips_analyzed`, `total_strokes`, and `outcomes.errors`; contract tests |
| P2 | Failed clips were counted as successful | Count clip status `ok`; mixed-result regression |
| P2 | Minted keys did not return the ID needed by the revoke endpoint | Return `key_id`; real create/revoke/auth test |
| P2 | Deployment baked in a previous Fly app, provisioned unmanaged Postgres implicitly, and reported success on failed health checks | Neutral template; bring-your-own DB default; explicit unmanaged opt-in; fail on readiness; mock-provider deployment tests |
| P2 | Provider commands could print newly provisioned secrets into CI logs | Private provisioning output file; secrets imported over stdin |
| P2 | Secret/media/build ignore rules were incomplete | Exclude environment files, generated credential files, raw media, weights, and build outputs from Git/Docker context |
| P2 | README conflated local privacy with cloud processing and overstated cost controls | Separate products/data flows; document estimates, lifecycle requirements, and integration responsibilities |

## Recommended next work

The handover centers on the API/worker and also includes the `cloud/` browser
prototype, its setup, tests, and [receiving-team acceptance checklist](../cloud/README.md#receiving-team-acceptance).
The seven follow-up items from the original review have the status below; browser
public-launch requirements remain distinct from handing over the private prototype.
API **0.2.0** requires a matching backend/configuration upgrade;
read [the upgrade checklist](../api/OPERATIONS.md#upgrade-from-the-original-handoff)
before deploying over an existing installation.

| Original item | Implemented in this repository | Receiving-team acceptance still required |
|---|---|---|
| 1. User access, quotas, hard provider budget | Required backend-delegated user ID; account/user ownership on reads and mutations; shared pending/upload/start limits; worker startup verifies a finite OpenRouter key budget | Derive the ID from verified app login, keep account keys on the backend, configure edge limits and the actual provider key |
| 2. Licensing, media rights, consent | Default image omits Torch/Ultralytics/YOLO; pose requires explicit build/runtime opt-in; sample provenance and provider data flow documented | Choose dependency/model terms, footage permissions, consent, and retention for the actual product; this review does not grant rights |
| 3. Durable accounting | Per-request reservations, provider cost settlement, failed/retried-attempt records, model allowlist, unresolved-cost inspection and generation reconciliation | Set a suitable hold/provider budget, reconcile unknown costs and historical estimates before charging; payment collection remains outside the service |
| 4. Recovery and delivery | Upload idempotency, URL refresh/list-parts, fenced source/options/code-validated clip checkpoints, durable leased webhook outbox with stable delivery IDs and admin replay | Persist upload plans, deduplicate callbacks, keep polling, and exercise device interruption/recovery |
| 5. Export/deletion/retention | Owner-authorized export and deletion, cancellation/tombstones, audited retrying cleanup of content/objects/versions/multipart/checkpoints/scratch, optional report retention, deletion-manifest replay after database restore | Configure S3 deletion/lifecycle permissions, backup expiry, provider retention, and report retention (default 0 preserves reports until explicit deletion) |
| 6. Schema/recovery/operations | Checksummed ordered migrations, legacy-upgrade and concurrent-admission/recovery tests, worker/queue/outbox/deletion/usage monitoring, tested backup/restore CLI; cleanup batches rotate past failures | Provision HA/PITR, alerts, worker storage, and load/capacity tests on the chosen infrastructure |
| 7. Conditional public browser launch | `cloud/` is private by default: production access gate, required origin configuration, mutation-origin checks, streamed body-size limit | Public launch remains a separate scope: verified identity, password recovery, signup/login throttling, spend limits, hosted request-size acceptance, and tenant tests against Neon |

Account keys are service credentials. `X-Courtside-User-Id` is trusted only because
the receiving backend supplies it after authenticating the app user; it is not an
end-user token verifier. Existing jobs need ownership backfill from that backend's
mapping. The private browser has its own database/login and does not inherit the
Python API's ledger, ownership, or deletion workflows.

Per-request holds are not guaranteed upper bounds on provider charges. Unknown
responses retain reservations, and legacy estimates remain labelled as estimates.
Clip checkpoints prevent repeating compatible completed clips, but interrupted
requests and report/moment generation can still incur repeat charges. Webhooks
can arrive more than once or exhaust retries. Live-store erasure does not erase
historical backups or downstream provider copies.

[Operations runbook](../api/OPERATIONS.md), [integration contract](../api/MOBILE_INTEGRATION.md),
[third-party notes](third-party.md), [receiving-team checklist](../HANDOFF.md).

## Validation limits

The following local checks passed for this update:

- **252** Python pipeline/API/regression tests.
- **18** real PostgreSQL 16 / MinIO integration tests, including concurrent upload
  idempotency and admission, ownership isolation, spend holds/settlement, checkpoint
  validation, outbox replay/recovery, versioned object and multipart erasure,
  cleanup fairness, legacy schema upgrade, and backup restoration with newer
  deletion tombstones.
- The integration suite runs the actual worker subprocess on synthetic ffmpeg
  video using a local HTTP provider stub and verifies usage records and published
  report downloads. It makes no paid model calls.
- **10** browser tests; production build and TypeScript check passed. A local
  production server also verified the private gate and Origin/body-size rejection.

- Current Linux arm64 default and optional pose images built successfully. The
  default image passed imports, absence-of-pose checks, fresh-volume UID 10001
  permissions, API 0.2.0 readiness against Postgres/S3, and the documented
  upload/complete/cancel smoke script. The optional image loaded its pose model.
- Generated OpenAPI matches source; local documentation links/anchors, Compose
  configurations, Python compilation, shell syntax, and whitespace checks passed.

The original handoff also recorded a clean browser dependency audit and image
checks on Linux amd64. This update's local image checks used Linux arm64; CI builds
the default image on amd64 after the changes are committed and pushed.

No real provider key, paid analysis, public endpoint, mobile app, live Fly
provisioning, production load test, or receiving-team backup system was exercised.
The Fly script is covered by mock CLI tests. CI configuration is updated, but a
remote CI run must follow the commit. Disposable service containers, test data
volumes, and the local browser test server were removed/stopped after verification.
These checks support a deployment/integration handoff; they do not certify
public-product quality, licensing, privacy policy, or production capacity.

## Default model

The API defaults to OpenRouter's `qwen/qwen3.8-27b`. Runtime defaults, Fly/Compose,
environment examples, the browser selection, and setup documentation agree.
The API records the selected model on queue admission. Both model clients request
non-thinking mode for Qwen3.8 and retain it through schema fallback. Omitting
`ALLOWED_MODELS` permits only the configured default. No comparative tennis-model
benchmark establishes this model as the best; the receiver should assess its own
footage and provider route.
