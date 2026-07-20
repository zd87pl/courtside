# courtside

Local tennis video analysis on Apple Silicon - a laptop-scale twin of the AceLens VLM
stage. Pure MLX, nothing leaves the machine.

**Pipeline:** motion-energy rally segmentation → capped frame sampling (~32 frames/clip)
→ per-clip stroke JSON from a local VLM (schema-validated) → aggregated Markdown
coaching report with monocular-video guardrails.

Built for: MacBook Pro M5 Max, 128 GB unified memory (614 GB/s). Works on smaller
machines with the smaller models.

Everything runs **on device** — no video, no frames, and no analysis ever leave the
machine. That's the point: youth-sports footage has minors on camera, and clubs care.
`--offline` makes it provable (see [Privacy / offline](#privacy--offline)).

## Quick start

```bash
./setup.sh
```

One interactive command: it detects your platform and RAM, installs ffmpeg + a venv +
the package with the right extras, helps you pick and pre-fetch a model, and then
launches either the **instant sample demo** (no model, always works) or a live run on
your own footage. Safe to re-run.

Prefer to see output before installing anything? Open
[`examples/demo_session/report.html`](examples/demo_session/) — a complete run you can
inspect without a Mac, a GPU, or a model download.

## Web app

```bash
courtside-ui        # serves http://127.0.0.1:8799 (localhost only)
```

A SaaS-style dashboard over the same pipeline: an overview hero with the on-device /
faster-than-realtime / $0-cloud story, **analyze from the browser** (pick a workspace
video, paste a path **or a YouTube URL**, choose a model), a **live phased progress view**
(Download → Segment → Extract → Analyze → Report) with a **Cancel** button and a streaming
log, and a rich per-session view — rally/stroke timeline with hover details, flagged-moments
keyframe gallery, per-rally cards, and the coaching report. Light and dark themes. One
analysis runs at a time (one laptop, one model). URL downloads fetch over the network via
yt-dlp; the analysis itself still runs entirely on-device (`--offline` refuses URLs).

The speed number is **honest end-to-end wall clock** — it includes model load, frame
extraction, inference, and the report pass, not just inference — so what the report claims
matches a stopwatch.

It is stdlib-only and binds to `127.0.0.1` — the "backend" is your laptop, nothing is
uploaded anywhere. `Export report` produces the self-contained `report.html` for sharing.

```bash
courtside-ui --root ~/tennis-videos   # scan a different workspace (repeatable)
```

## Setup (manual)

```bash
brew install ffmpeg
cd courtside
python3.11 -m venv .venv && source .venv/bin/activate   # 3.11+ (matches requires-python)
pip install -e '.[local]'   # Apple Silicon; core-only off-Mac: pip install -e '.[server]'
```

`mlx-vlm` is an Apple-Silicon-only extra (`[local]`), so the model-free parts —
segmentation, schema, reports — install and test on any machine. First run of a model
downloads weights from Hugging Face (set `HF_HOME` for external storage); pre-fetch with
`courtside --prefetch qwen3-vl-32b`.

## Model selection (128 GB M5 Max)

| key | repo | ~weights | when to use |
|---|---|---|---|
| `qwen3-vl-32b` **(default)** | mlx-community/Qwen3-VL-32B-Instruct-8bit | ~35 GB | best quality/headroom balance |
| `qwen3-vl-32b-thinking` | ...-Thinking-8bit | ~35 GB | harder tactical reasoning, slower |
| `qwen3-vl-30b-a3b` | Qwen3-VL-30B-A3B-Instruct-8bit | ~32 GB | MoE, fastest decode, iteration |
| `qwen3-vl-8b` | Qwen3-VL-8B-Instruct-8bit | ~9 GB | smoke tests (video ≈ Qwen2.5-VL-72B class) |
| `glm-4.6v-flash` | lmstudio-community/GLM-4.6V-Flash-MLX-8bit | ~10 GB | second-opinion architecture |
| `qwen3-vl-235b` | Qwen3-VL-235B-A22B-Instruct-3bit | ~97 GB | stretch flagship, see below |

Any HF repo or local path also works: `--model mlx-community/...`.

### Running the 235B flagship on 128 GB

3-bit fits; 4-bit does not. macOS caps GPU-wired memory at ~75% of RAM by default -
raise it for the session, and quantize the KV cache:

```bash
sudo sysctl iogpu.wired_limit_mb=117760     # ~115 GB; resets on reboot
courtside match.mp4 --model qwen3-vl-235b --kv-bits 4 --max-frames 24 --max-side 672
```

If the exact 3-bit mlx-community quant isn't published, convert locally:
`python -m mlx_vlm.convert --hf-path Qwen/Qwen3-VL-235B-A22B-Instruct -q --q-bits 3`.

## Usage

```bash
# plumbing check - segmentation + frame extraction only, no model load
courtside match.mp4 --dry-run

# full run with defaults
courtside match.mp4

# analyze straight from YouTube (needs the [youtube] extra; downloads, then runs locally)
courtside "https://youtube.com/watch?v=..." --max-clips 3

# quick pass: first 3 clips, small model, tokens streaming live
courtside match.mp4 --model qwen3-vl-8b --max-clips 3 --stream

# re-render the report from a finished run - ZERO model calls (instant demo)
courtside --from-dir match_courtside

# resume a crashed/interrupted run, skipping clips already analyzed
courtside match.mp4 --resume

# compare models on your own footage (persist outputs for a side-by-side)
courtside-bench match.mp4 --models qwen3-vl-8b qwen3-vl-30b-a3b qwen3-vl-32b --out bench_out
```

Outputs land in `<video>_courtside/`: per-clip frames + `clip_NNN.json`, a
`session.json` envelope (provenance + run metrics + every clip's analysis),
`session_report.md`, and a self-contained **`report.html`** — the visual report with a
flagged-moments keyframe gallery, a rally/stroke timeline, and an on-device cost line.

### Reliability & live-demo notes

The pipeline is built to survive a stage: one bad clip is skipped (never aborts the run),
the report is always written from whatever succeeded, `--resume` continues a crashed run,
and `--from-dir` re-renders instantly with no model. Missing ffmpeg, a cold model cache,
or too little RAM are caught up front with a clear message instead of a late traceback.

**Demo runbook:** rehearse the night before (that run becomes your cached asset); on the
day, run airplane-mode with `--offline`, analyze 1-2 clips live on `qwen3-vl-30b-a3b`
(fastest decode) with `--stream`, then open the pre-baked `report.html`.

### Privacy / offline

`--offline` sets `HF_HUB_OFFLINE=1` so the run cannot touch the network (weights must be
pre-cached via `courtside --prefetch <model>`). The report is stamped
*"Processed entirely on this device — network not used."* Do the live demo with Wi-Fi off.

### Server mode (constrained decoding)

The in-process path validates + repairs JSON with Pydantic. Against an OpenAI-compatible
server (mlx-vlm's server, or LM Studio serving the same MLX models) courtside also sends a
strict-mode-compatible JSON schema for constrained decoding, so servers that honor it emit
schema-valid output directly:

```bash
# terminal 1
mlx_vlm.server --model mlx-community/Qwen3-VL-32B-Instruct-8bit --port 8080

# terminal 2
courtside match.mp4 --server-url http://localhost:8080/v1 \
  --server-model mlx-community/Qwen3-VL-32B-Instruct-8bit
```

courtside pings the server before starting and uses a request timeout, so an unreachable
endpoint fails fast instead of hanging. (Not every server enforces strict mode; the
Pydantic validate-and-repair path is always the backstop.)

## Tuning knobs

- `--fps` / `--max-frames` (default 4.0 / 32): the research sweet spot - too few frames
  misses contacts, every-frame hurts. `fps` auto-reduces so long clips still cap at 32.
- `--max-side` (default 784 px): controls vision tokens per frame (~(side/28)² · AR for
  Qwen-class). Drop to 672 to trade detail for speed/memory.
- `--segment fixed --window 20` if the motion detector misfires on your footage
  (moving camera, indoor lighting), or `--segment file --segments-file segs.json`
  with `[[start_s, end_s], ...]` for hand-picked rallies.
- `--kv-bits 8` (or 4) quantizes the KV cache - useful for the 235B or very long clips.

## What this demo deliberately does NOT do

Per the CalTennis findings, single-camera video cannot reliably measure absolute
depth/distance, foot contact, weight transfer, or forces. The prompts forbid those
claims and every report carries an auto-appended limitations section. Stroke counts
are lower bounds (sampling at 4 fps can miss contacts) - the SaaS pipeline replaces
this with a dedicated event-spotting model (F3ED) precisely because VLMs can't count
rally events reliably.

## Mapping back to AceLens

The clip JSON schema here is intentionally a subset of the AceLens `Rally/Stroke/Error`
shared types, so prompts and eval assets transfer directly. The full story — which stages
transfer as-is vs. get swapped at scale, and why — is in
**[docs/architecture.md](docs/architecture.md)** (with a diagram):

| courtside (demo) | AceLens (production) |
|---|---|
| motion-energy segmentation | rally/event detection worker (F3ED) |
| ffmpeg frame sampling | ingest/preprocess worker on RunPod |
| VLM sees raw frames | VLM sees structured JSON from CV stack + keyframes |
| Pydantic validate + 1 retry | server-side constrained decoding |
| session_report.md + report.html | coach dashboard report + drill refs |
