# courtside

Local tennis video analysis on Apple Silicon - a laptop-scale twin of the AceLens VLM
stage. Pure MLX, nothing leaves the machine.

**Pipeline:** motion-energy rally segmentation → capped frame sampling (~32 frames/clip)
→ per-clip stroke JSON from a local VLM (schema-validated) → aggregated Markdown
coaching report with monocular-video guardrails.

Built for: MacBook Pro M5 Max, 128 GB unified memory (614 GB/s). Works on smaller
machines with the smaller models.

## Setup

```bash
brew install ffmpeg
cd courtside
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .            # add: pip install -e '.[server]' for server mode
```

First run of a model downloads weights from Hugging Face (set `HF_HOME` if you want
them on external storage).

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

# quick pass: first 3 clips, small model
courtside match.mp4 --model qwen3-vl-8b --max-clips 3

# compare models on your own footage
courtside-bench match.mp4 --models qwen3-vl-8b qwen3-vl-30b-a3b qwen3-vl-32b
```

Outputs land in `<video>_courtside/`: per-clip frames + `clip_NNN.json`,
`session.json`, and `session_report.md`.

### Server mode (strict JSON schema)

The in-process path validates + repairs JSON with Pydantic. For *guaranteed*
schema-valid output, run against an OpenAI-compatible server with constrained
decoding (mlx-vlm's server, or LM Studio serving the same MLX models):

```bash
# terminal 1
mlx_vlm.server --model mlx-community/Qwen3-VL-32B-Instruct-8bit --port 8080

# terminal 2
courtside match.mp4 --server-url http://localhost:8080/v1 \
  --server-model mlx-community/Qwen3-VL-32B-Instruct-8bit
```

Server mode also gets you continuous batching and vision-feature caching for free.

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

| courtside (demo) | AceLens (production) |
|---|---|
| motion-energy segmentation | rally/event detection worker (F3ED) |
| ffmpeg frame sampling | ingest/preprocess worker on RunPod |
| VLM sees raw frames | VLM sees structured JSON from CV stack + keyframes |
| Pydantic validate + 1 retry | server-side constrained decoding |
| session_report.md | coach dashboard report + drill refs |

The clip JSON schema here is intentionally a subset of the AceLens `Rally/Stroke/Error`
shared types, so prompts and eval assets transfer directly.
