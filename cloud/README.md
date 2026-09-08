# Courtside browser prototype

An independent browser proof of concept for Courtside: accounts for **coaches and players**, **teams for
schools** with invite codes, browser-side video processing, OpenRouter-powered analysis,
and Postgres persistence. This directory is a self-contained Next.js app with
its own accounts and database.

## Handoff scope

This prototype **is included in the repository handover**, alongside the primary
API/worker service. The receiving team receives its Next.js source, dependency
lockfile, environment template, tests, and this setup/acceptance guide. It can be
deployed privately for demonstrations, evaluation, and further development.

This app does not call the Python mobile API, share its account keys, or display
its jobs. For mobile integration use [api/MOBILE_INTEGRATION.md](../api/MOBILE_INTEGRATION.md).
Production now requires the private access gate described below and refuses
requests when its configuration is missing. Public signup/login throttling, verified
identity, password recovery, spend controls, and Neon authorization acceptance tests
remain required before a public launch; see [the review](../docs/review.md).

| Component | Browser prototype setup |
|---|---|
| Runtime | Next.js on Node.js 22.13+ or 24, with HTTPS in deployment |
| Database | Your own Neon database, separate from the Python API database; `DATABASE_URL` or `POSTGRES_URL` |
| Model | Your server-side `OPENROUTER_API_KEY`; default `qwen/qwen3.8-27b` |
| Private access | `CLOUD_ACCESS_TOKEN` of at least 32 characters and `APP_ORIGIN` matching the deployed origin |
| User accounts | Created through the prototype's sign-up screen; independent of Courtside API account keys |
| Storage/worker | No Python worker or API S3 bucket is used by this application |

## Deploy to Vercel

1. **Import the repo** in Vercel → *Add New Project*.
2. Set **Root Directory** to `cloud` (Framework Preset: Next.js is auto-detected).
3. In the project's **Storage** tab, add the **Neon (Postgres)** integration —
   `DATABASE_URL` is injected automatically. The schema bootstraps itself on first
   request (no migrations to run).
4. In **Settings → Environment Variables**, add:
   - `OPENROUTER_API_KEY` — your OpenRouter key (required)
   - `OPENROUTER_MODEL` — optional, defaults to `qwen/qwen3.8-27b`
   - `CLOUD_ACCESS_TOKEN` — random secret of at least 32 characters, shared only with private testers
   - `APP_ORIGIN` — exact HTTPS origin, e.g. `https://your-project.vercel.app`
5. **Deploy privately.** Open the site and enter HTTP Basic username `courtside`
   and the access token as password. Then use the app's own sign-up/login to test
   coaching and team flows. The gate is separate from those app accounts.

The browser prototype uses the same Qwen3.8 27B default as the mobile API. Its
optional override is named `OPENROUTER_MODEL` (the Python API uses `DEFAULT_MODEL`).
Only the server reads the provider key; never use a `NEXT_PUBLIC_` key variable.
For Qwen3.8 27B, requests disable thinking with `reasoning: {"effort": "none"}`
so the output budget goes to results. See the [model details](../api/README.md#default-llm).

Production without the gate/origin settings returns 503. Mutating requests must
carry the configured Origin, and streamed JSON bodies are capped at 3.5 MB before
route processing. Host limits may be lower; test representative frame batches
on your deployment. Local `npm run dev` omits the private gate but retains the
origin/body checks. This prototype does not use the Python API's usage ledger.

## How analysis works in the cloud

The uploaded video **never leaves the browser**. The client decodes it, runs the same
motion-energy rally segmentation as the local demo (ported to TypeScript), and samples
JPEG frames from each rally. Only those frames are sent to `/api/analyze`, which calls
OpenRouter with the same prompts, schema, and validate-and-repair behavior as the local
pipeline, then `/api/sessions/finalize` aggregates facts and writes the coaching report.
Sessions (analyses + report + keyframe thumbnails) persist in Postgres.

## Features provided by the Python pipeline

Biomechanics overlays, ghost comparisons, slow-motion clips, and strike-zone measurement
require pose models and ffmpeg through the Python CLI pose extra or the explicitly
opted-in API pose image;
this browser prototype does not implement them. Use the
[Python API](../api/README.md) to integrate that pipeline into an app.

## Local development

Use Node.js 22 LTS (22.13+) or 24 LTS.

```bash
cd cloud
npm ci
cp .env.example .env.local   # fill in OPENROUTER_API_KEY and a Neon DATABASE_URL
npm run dev
```

Run the browser checks independently from the Python suite:

```bash
npm test
npm run build
npm run typecheck
```

`npm test` covers shared logic (segmentation, JSON extraction, facts), model request
settings, and request guards. These checks do not connect to your Neon database or
verify paid inference.

`npm run build` checks the production bundle/types; `npm audit --audit-level=high`
checks known dependency advisories. Use the checked-in lockfile. PostCSS is
overridden to a patched release; reevaluate that override when upgrading Next.js.
Use your own Neon database, separate from the API database. Initial schema creation
is automatic; future table changes require a migration plan. Apply the same media
consent/retention controls to persisted thumbnails and model-bound frames.

## Receiving-team acceptance

Use a private deployment and a separate test Neon database with the receiving
team's credentials. No original developer account or pre-existing demo login is
required. The following checks exercise the prototype itself, separately from the
Python API smoke script:

1. Confirm the deployment challenges unauthenticated visitors for the private
   gate. Enter username `courtside` and the configured access token. Verify the
   production origin and HTTPS URL, including any custom-domain change.
2. Sign up as a coach, create a team, then sign up as a player in a separate
   browser profile and join with the team's invite code. Check login/logout and
   persistence across a browser restart. App logout ends the app session; the
   browser may retain the separate HTTP Basic gate credentials.
3. With a short video you have permission to process, create a session and run
   analysis. **This uses provider credits.** Inspect the report and thumbnails,
   reload the report, and verify it appears for the intended coach/player.
4. Use another unrelated test account to check that private session pages and
   team/session mutations do not grant access outside its permissions. These
   hosted authorization checks remain necessary; the local logic suite does not
   exercise a real Neon deployment.
5. Record the browser URL, source commit, database/project owner, model/provider
   configuration, secret-rotation owner, backup policy, and test-data cleanup
   procedure alongside the API handover record. Keep credentials outside Git.

The browser has no durable worker queue, portable analysis checkpoints, API usage
ledger, or customer export/deletion workflow. The API's migrations and database
restore/deletion scripts apply only to the API schema. Give the prototype its own
database backup/retention procedure and keep private test data limited to its
intended use. Moving this UI onto the Python API, or opening it as a public product,
requires further development; neither connection is implied by transferring both.
