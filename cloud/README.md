# Courtside Cloud (Vercel)

The SaaS deployment of Courtside: accounts for **coaches and players**, **teams for
schools** with invite codes, browser-side video processing, OpenRouter-powered analysis,
and Postgres persistence. The Python package at the repo root (the on-device demo) is
untouched and unaffected — this directory is a self-contained Next.js app.

## Deploy to Vercel (~5 minutes)

1. **Import the repo** in Vercel → *Add New Project*.
2. Set **Root Directory** to `cloud` (Framework Preset: Next.js is auto-detected).
3. In the project's **Storage** tab, add the **Neon (Postgres)** integration —
   `DATABASE_URL` is injected automatically. The schema bootstraps itself on first
   request (no migrations to run).
4. In **Settings → Environment Variables**, add:
   - `OPENROUTER_API_KEY` — your OpenRouter key (required)
   - `OPENROUTER_MODEL` — optional, defaults to `qwen/qwen2.5-vl-72b-instruct`
5. **Deploy.** Sign up as a Coach, create a team, share the invite code; players sign up
   and join with it.

## How analysis works in the cloud

The uploaded video **never leaves the browser**. The client decodes it, runs the same
motion-energy rally segmentation as the local demo (ported to TypeScript), and samples
JPEG frames from each rally. Only those frames are sent to `/api/analyze`, which calls
OpenRouter with the same prompts, schema, and validate-and-repair behavior as the local
pipeline, then `/api/sessions/finalize` aggregates facts and writes the coaching report.
Sessions (analyses + report + keyframe thumbnails) persist in Postgres.

## What's deliberately local-only (for now)

Biomechanics overlays, ghost comparisons, slow-motion clips, and strike-zone measurement
require pose models and ffmpeg — they run in the on-device demo (`courtside` /
`courtside-ui` at the repo root). That split is the product story: the cloud tier is the
team SaaS; the on-device tier is the private, full-fidelity analysis engine.

## Local development

```bash
cd cloud
npm install
cp .env.example .env.local   # fill in OPENROUTER_API_KEY and a Neon DATABASE_URL
npm run dev
```

`npm test` runs the logic parity tests (segmentation port, JSON extraction, facts).
