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

1. **Before public mobile access:** implement user-level authentication/authorization
   in the receiving backend, quotas/rate limits for reservations and job starts,
   and a model-provider hard budget. Account keys are service credentials. Neither
   the API's estimated spend cap nor the browser's login is a billing/abuse system.
2. **Before commercial distribution:** resolve licensing for bundled pose code and
   weights, sample footage rights, and retention/consent requirements for the actual
   provider flow. See `third-party.md`; this review does not decide licensing terms.
3. **Before charging by usage:** implement a durable spend ledger with in-flight
   reservations, actual provider usage, failed-attempt accounting, and model allowlists.
   The current cost is a rough token-based estimate and can undercount or overshoot.
4. **For stronger delivery/retry guarantees:** implement reservation idempotency,
   upload URL refresh/list-parts, resumable analysis checkpoints, and an outbox-based
   webhook dispatcher. Current callbacks are best effort; polling is authoritative.
5. **For customer deletion/export workflows:** add owner-authorized deletion endpoints
   and auditable retention jobs covering Postgres, uploads, reports, object versions,
   scratch, and backups. Bucket lifecycle currently handles abandoned data.
6. **Before schema evolution/high availability:** introduce versioned migrations,
   load/recovery tests, worker-capacity monitoring, and tested backup restoration.
   Initial schema bootstrap does not upgrade existing table shapes.
7. **If launching `cloud/` publicly:** add signup/login throttling, verified identity,
   password reset, spend/rate limits, request-size limits appropriate to the hosting
   platform, CSRF/origin defenses, and end-to-end tenant authorization tests against
   Neon. It remains an independent proof of concept, not a mobile production console.

## Validation limits

Model-free pipeline/API tests, real local Postgres/MinIO contract tests, the browser
production build, dependency audit, and container runtime checks are automated or
recorded in this handoff. Docker Compose is for local/own-host testing; the Fly
script is tested with a mock CLI and needs recipient-account acceptance. No real
provider key, paid analysis, public endpoint, or mobile app was exercised during
this review. CI configuration was updated; a remote CI run must follow the commit.

Recorded local results:

- 236 Python pipeline/API/regression tests passed (Python 3.11).
- 3 real Postgres 16 / MinIO integration tests passed, including multipart retry.
- 8 browser logic tests passed, including a Node 22 container run; production
  build and standalone TypeScript check passed. Local build used Node 25;
  CI is configured to build on supported Node 22.
- Browser `npm audit`: zero reported vulnerabilities after the locked updates.
- Linux arm64 and amd64 deployment images built with the pinned dependencies;
  runtime imports and bundled pose-model loading passed on both architectures.
- Fresh-volume ownership/non-root execution, container readiness, and the supplied
  upload/complete/cancel smoke script passed. Temporary service containers and test
  database/storage volumes were removed afterward.
- OpenAPI JSON parsed; local documentation links, Compose configurations, shell
  syntax, and whitespace checks passed. A current-source credential-pattern scan
  found no matches; this was not a full Git-history secret audit.

## Default model follow-up

The API now defaults to OpenRouter's `qwen/qwen3.8-27b`.
Runtime defaults, Fly/Compose settings, environment examples, the browser model
selection, and setup documentation agree. The selected model is recorded on
queue admission, and missing provider credentials fail before worker DB startup.
For Qwen3.8 27B, both model clients request non-thinking mode and retain it through
schema fallback, avoiding an implicit return to the model's default thinking mode.
Regression coverage checks omitted/blank configuration, operator/job overrides,
deployment defaults, the queued response, frame requests, and provider rejection.
Updated verification: 247 Python tests passed; 9 browser tests, production build,
TypeScript check, Compose model
configuration, and OpenAPI documentation checks passed. No paid inference or
comparative tennis-model benchmark was run for this change.
