# Courtside browser prototype

An independent browser proof of concept for Courtside: accounts for **coaches and players**, **teams for
schools** with invite codes, browser-side video processing, OpenRouter-powered analysis,
and Postgres persistence. This directory is a self-contained Next.js app with
its own accounts and database.

## Handoff scope

This app does not call the Python mobile API, share its account keys, or display
its jobs. For mobile integration use [api/MOBILE_INTEGRATION.md](../api/MOBILE_INTEGRATION.md).
Keep this prototype behind deployment access controls until the public-launch
items in [the review](../docs/review.md) are implemented: auth recovery/verification,
rate limits, usage controls, and hosted request-size testing.

## Deploy to Vercel

1. **Import the repo** in Vercel → *Add New Project*.
2. Set **Root Directory** to `cloud` (Framework Preset: Next.js is auto-detected).
3. In the project's **Storage** tab, add the **Neon (Postgres)** integration —
   `DATABASE_URL` is injected automatically. The schema bootstraps itself on first
   request (no migrations to run).
4. In **Settings → Environment Variables**, add:
   - `OPENROUTER_API_KEY` — your OpenRouter key (required)
   - `OPENROUTER_MODEL` — optional, defaults to `qwen/qwen3.8-27b`
5. **Deploy.** Sign up as a Coach, create a team, share the invite code; players sign up
   and join with it.

The browser prototype uses the same Qwen3.8 27B default as the mobile API. Its
optional override is named `OPENROUTER_MODEL` (the Python API uses `DEFAULT_MODEL`).
Only the server reads the provider key; never use a `NEXT_PUBLIC_` key variable.
For Qwen3.8 27B, requests disable thinking with `reasoning: {"effort": "none"}`
so the output budget goes to results. See the [model details](../api/README.md#default-llm).

## How analysis works in the cloud

The uploaded video **never leaves the browser**. The client decodes it, runs the same
motion-energy rally segmentation as the local demo (ported to TypeScript), and samples
JPEG frames from each rally. Only those frames are sent to `/api/analyze`, which calls
OpenRouter with the same prompts, schema, and validate-and-repair behavior as the local
pipeline, then `/api/sessions/finalize` aggregates facts and writes the coaching report.
Sessions (analyses + report + keyframe thumbnails) persist in Postgres.

## Features provided by the Python pipeline

Biomechanics overlays, ghost comparisons, slow-motion clips, and strike-zone measurement
require pose models and ffmpeg. They run in the Python CLI and deployed API worker;
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

`npm test` runs the logic parity tests (segmentation port, JSON extraction, facts).

`npm run build` checks the production bundle/types; `npm audit --audit-level=high`
checks known dependency advisories. Use the checked-in lockfile. PostCSS is
overridden to a patched release; reevaluate that override when upgrading Next.js.
Use your own Neon database, separate from the API database. Initial schema creation
is automatic; future table changes require a migration plan. Apply the same media
consent/retention controls to persisted thumbnails and model-bound frames.
