# Courtside — Repository Analysis & Investor-PoC Readiness Plan

*Analysis date: 2026-07-19. Method: full manual code review, plus a 92-agent multi-dimension
review (correctness, API compatibility, live-demo risk, investor-facing gaps, repo hygiene)
with two independent adversarial verifiers per technical finding. Everything runnable off-Mac
was verified by execution (Pydantic schema generation, segmentation logic on synthetic
activity curves, the exact ffmpeg command on a test video, mlx-vlm 0.6.5 API surface,
HuggingFace repo existence). Findings below are labeled with file:line anchors.*

---

## Executive summary

The codebase is a genuinely good PoC skeleton: ~900 lines, clean module boundaries, honest
scientific guardrails (monocular-video limitations enforced in prompts *and* appended
statically), a real model registry (all six HF repos verified to exist), and mlx-vlm usage
that matches the current 0.6.5 API. The pipeline design — cheap CV segmentation before the
expensive VLM — is the right story to tell investors.

What stands between this and a safe investor demo is **not** model quality. It is:

1. **Fragility**: one bad clip, one dropped connection, or one truncated model output kills
   the entire run with a traceback and *no report at all* — the exact failure mode you
   cannot afford on stage.
2. **No replay**: there is no way to re-render a report from a previous successful run, so
   every demo is a live, multi-minute, all-or-nothing model run.
3. **No visual layer**: the deliverable is a plain Markdown file, while every ingredient for
   a compelling visual report (flagged keyframes, timestamps, severities, evidence strings)
   is already produced and then left on disk unused.

The good news: the P0 fixes are roughly **one day of work**, and the highest-impact demo
upgrades (HTML report + cached demo mode + sample output) are **3–4 days**.

---

## 1. What's already strong

- **Architecture**: segmentation → capped frame sampling → schema-validated JSON → report is
  the correct laptop-scale mirror of the production design, and `segment.py`'s docstring
  honestly frames motion energy as a stand-in for the real event pipeline.
- **Scientific honesty**: `prompts.py` forbids claims monocular video can't support, and
  `LIMITATIONS_FOOTER` is appended unconditionally. Due-diligence reviewers notice this.
- **Verified-correct externals**: the ffmpeg `-ss/-to` input-option invocation in
  `frames.py:47-54` is exactly right (tested: 8 s window → 32 frames @ 4 fps); all six
  registry repos in `config.py` exist on HF Hub; `vlm.py`'s `stream_generate` /
  `apply_chat_template` / `GenerationResult` usage matches mlx-vlm 0.6.5, including float
  `kv_bits`.
- **Schema discipline**: Pydantic models with `extra="forbid"`, a bounded flag vocabulary
  concept, and intentional subset-of-production types.

---

## 2. P0 — will break or embarrass a live demo

### 2.1 One failed clip kills the entire run *(critical, confirmed)*
`analyze.py:159-167` has no per-clip error handling. The repair retry's second
`model_validate` (`analyze.py:69`) is unprotected, the initial `vlm.generate`
(`analyze.py:53`) is outside the try entirely, and the frame-extraction loop
(`analyze.py:134-138`) is equally bare (`frames.py:54` `check=True`, `frames.py:58`
RuntimeError). `session.json` and `session_report.md` are only written after **all** clips
succeed — so clip 14 of 20 failing twice means a traceback and zero deliverable, even though
13 valid `clip_NNN.json` files sit on disk. Notably, `bench.py:62` already contains the
per-item `except` pattern; it was just never applied to the main pipeline.

**Fix (<1 h):** wrap both loops per-clip (`log + continue`), require ≥1 success, always
reach the report step, record skipped clips in Confidence Notes.

### 2.2 No replay, resume, or caching *(critical, confirmed)*
The only non-model path is `--dry-run`. Report generation is fused to inference, so a
crashed run — or a rehearsal you'd like to replay — restarts from zero: re-extraction and
full re-inference. Standard PoC practice is to run 1–2 clips live for authenticity, then
flip to a pre-baked full session.

**Fix (2–4 h):** `--from-dir <out_dir>` to reload existing `clip_NNN.json` and re-render
`session.json` + report with no model calls; `--resume` to skip clips whose JSON exists.
This plus 2.1 makes the live path effectively crash-proof.

### 2.3 First run silently downloads ~35 GB, after minutes of wasted work *(critical, confirmed)*
`LocalVLM` loads **after** segmentation and frame extraction, so a cold HF cache surfaces
minutes into the run — on conference Wi-Fi, that's the demo. There's also no RAM pre-flight
despite `ModelSpec.approx_weights_gb` sitting right there (`config.py:21`): the default
35 GB model on a borrowed 36/64 GB MacBook means Metal OOM or swap-death.

**Fix (2–3 h):** pre-flight `snapshot_download(repo, local_files_only=True)` before any
work; on miss, print the size and bail with a `--prefetch` hint. Compare
`approx_weights_gb × ~1.3` against RAM. For demo day: `HF_HUB_OFFLINE=1`.

### 2.4 Dead air — long operations look like a hang *(high, confirmed)*
Model load shows a static `(loading...)`; each clip's 20–60 s generation prints nothing
until done — yet `LocalVLM.generate` already consumes a token stream (`vlm.py:90-92`) and
throws away the live text. On stage, silence reads as "it crashed."

**Fix (2–3 h):** echo streamed tokens (dim) or an in-place progress line
(`clip 3/12 · 412 tok · 14.2s`). "The model is writing its scouting notes in real time" is
itself a good demo beat.

### 2.5 Stale frames silently contaminate re-runs *(high, confirmed)*
`extract_clip_frames` reuses `clip_NNN` dirs (`frames.py:37`), ffmpeg `-y` only overwrites
same-numbered files, and `sorted(out_dir.glob("frame_*.jpg"))[:max_frames]` (`frames.py:56`)
globs *everything*. Rehearse at one fps, run live at another → the VLM quietly analyzes a
mix of old and new frames from wrong time ranges.

**Fix (one line):** clear `frame_*.jpg` (or rmtree the clip dir) before extraction.

### 2.6 Server-mode "strict schema" doesn't hold *(high, verified by execution — nuance noted)*
`clip_json_schema()` output is not OpenAI-strict-compliant: `required` omits every
defaulted field (`strokes`, `notes`, `technique_flags`, `tactical_flags`) and the schema
carries `maxLength`/`default` keywords. A conformant strict implementation rejects it (400,
uncaught → run dies); mlx_vlm.server, per the verifiers, doesn't enforce strict mode at all
— meaning the README's *"guaranteed schema-valid output"* claim is unearned either way.

**Fix (1–2 h):** build a strict-compatible schema variant (all fields required, constraints
moved to prompt text or dropped) for server mode, or remove `strict: True` and rely on the
existing validate+repair path. Also: `ServerVLM` has no request timeout and no connectivity
pre-flight — a dead server hangs for the SDK default. Add `timeout=`, one startup ping, and
catch per-clip (2.1).

### 2.7 Thinking-model output leaks into the deliverable *(high, confirmed)*
Only `extract_json` strips `<think>` blocks; the report pass (`analyze.py:173-178`) writes
raw model text. With the registry's `qwen3-vl-32b-thinking`, `session_report.md` opens with
chain-of-thought — or is entirely thinking, truncated at 2 400 tokens. Also verified by
execution: an *unclosed* `<think>` (max_tokens cutoff) defeats the regex and `extract_json`
can parse JSON found *inside* the reasoning as if it were the answer.

**Fix (1 h):** strip think blocks (including close-only/unclosed forms) on every generate
path; for demo day, simply don't use the thinking variant.

---

## 3. P1 — correctness and robustness

| # | Finding | Anchor | Note |
|---|---------|--------|------|
| 1 | Splitter emits tail fragments shorter than `min_rally_s` — length filter runs before the split | `segment.py:104-114` | Reproduced: 2.2 s clip survives a 2.5 s minimum |
| 2 | Purely relative thresholds: constant-motion footage (handheld) → 100 % of video "active", silently sent to VLM | `segment.py:81-82` | Reproduced with synthetic curve; opposite failure of the documented "0 segments" case |
| 3 | Zero segments exits code 1 with a hint — no auto-fallback, though `bench.py:40` already has the `or fixed_windows(...)` pattern | `analyze.py:128-130` | |
| 4 | Missing ffmpeg/ffprobe → raw `FileNotFoundError` as the first pipeline action; `probe_duration` crashes on `N/A` durations | `frames.py:54,62-68` | Pre-flight `shutil.which` + friendly message |
| 5 | `CAP_PROP_FRAME_COUNT` can be 0/−1 for some containers → zero/negative duration, everything silently dropped | `segment.py:38,63` | Fall back to ffprobe duration |
| 6 | Repair retry runs with `images=None` — "repaired" analyses are reconstructed without seeing the frames; the repair prompt also never includes the schema it references | `analyze.py:64-68` | Re-send frames + schema, or skip the clip |
| 7 | `Stroke.t_s` unvalidated — clip-relative vs absolute confusion passes silently and poisons report citations | `schema.py:34` | Clamp/validate against `[start_s, end_s]`; frames are also sent unlabeled while `ClipFrames.timestamps()` (`frames.py:25-26`) is dead code |
| 8 | Report prompt outsources arithmetic (stroke totals, near/far split, flag rankings) to the LLM though Python has ground truth | `prompts.py:56-67` | Compute in Python, inject as facts |
| 9 | `Flag.code` is free-form — aggregation fragments across synonyms (`late_preparation` / `late_prep`) | `schema.py:26` | Enum the ~15 codes the prompt already names |
| 10 | Report input unbounded, output capped at 2 400 tokens, no truncation/section check; `LIMITATIONS_FOOTER` hardcodes "~0.25 s" resolution (false for capped clips, up to ~1.4 s) | `analyze.py:173-176`, `prompts.py:103` | |
| 11 | `bench.py`: `max()` crashes on empty segment list; failed model not released before the next loads; outputs discarded (see §4.5) | `bench.py:41,54-63` | |
| 12 | `fixed_windows(--window ≤ 0)` loops forever; `from_file` segments unvalidated (end ≤ start, beyond-EOF) | `segment.py:118-140` | |
| 13 | Clip prompt rounds fps to one decimal — frame-to-time mapping skews up to ~0.7 s on capped clips | `prompts.py:19-21` | Print full precision |

---

## 4. Investor-demo gap analysis (the part that makes the demo land)

The pipeline's outputs are currently invisible. Every item below reuses artifacts the code
already produces.

### 4.1 Visual HTML report *(highest impact — ~1–2 days)*
Frames persist per clip; every `Flag` has severity + evidence; every `Stroke` has `t_s`;
`ClipFrames.timestamps()` gives the frame↔time mapping and is currently called by nothing.
A single self-contained HTML file (inline CSS, base64 JPEGs, stdlib only): session header →
per-clip cards with thumbnails → **flagged-moments gallery** (the frame of the late
backhand preparation, severity chip, model's evidence quote) → the Markdown report at the
bottom. An investor watching a terminal print `12 strokes, 7 flags` sees nothing; the same
data as annotated frames *is* the product.

### 4.2 Cached instant-demo mode *(2–4 h — do this first)*
§2.2's `--from-dir` replay. Demo runbook: live-run 1–2 clips on the fast MoE model for
authenticity, then open the pre-baked full-session HTML report. Rehearsals become assets
instead of throwaway compute.

### 4.3 Committed sample output *(1–2 h)*
`examples/demo_session/` with 2–3 real clip JSONs, `session.json`, the report, ~6 downscaled
frames, linked at the top of the README. Right now a diligence analyst on a Linux laptop
cannot produce or see a single byte of output — `pip install -e .` doesn't even succeed
off-Mac (§5.1). This is the cheapest possible credibility artifact. Use footage you have
rights to.

### 4.4 The metrics investors actually ask *(3–5 h)*
`analyze_clip` already returns wall time, token counts, and peak memory (`analyze.py:73-79`)
— then the caller prints and discards them. Aggregate into a `run_stats` block in
`session.json` and a closing line:
*"Processed 38 min of footage in 11 min (3.4× realtime) — 214 k tokens — cloud-API
equivalent ≈ $1.87, on-device cost $0.00, 0 bytes uploaded."*
That one line answers the two questions ("cost vs cloud?", "how long per hour of footage?")
every on-device pitch gets.

### 4.5 Side-by-side model comparison *(~1 day)*
`courtside-bench` already runs identical prompt+frames across models but throws away the
generated analyses (`bench.py:55`) and keeps only perf numbers. Persist outputs and render
one page: clip thumbnails on top, one column per model — *watch the 8B miss the late
preparation the 32B catches*. Perf tables impress engineers; divergent qualitative output on
the investor's own footage impresses investors.

### 4.6 Make the privacy differentiator visible *(2–3 h)*
"Nothing leaves the machine" (README line 4) is the thesis and appears in zero product
output. Add an on-device header to the report ("Processed entirely on this device — network
not used"), an `--offline` flag setting `HF_HUB_OFFLINE=1`, and do the live demo in
airplane mode. Youth-sports footage (minors on camera) makes this framing land hard.

### 4.7 Rally/stroke timeline *(~1 day, on top of 4.1)*
`_activity_curve` computes a motion-energy series and throws it away (`segment.py:77`).
Dump it to `activity.json`, render an SVG timeline: activity sparkline, rally blocks, stroke
ticks colored by max flag severity, click-through to clip cards. One graphic that explains
segment → analyze → flag in five seconds.

### 4.8 Flagged-moment video snippets *(0.5–1 day)*
Technique flaws are motion phenomena; a 3 s loop beats a JPEG. The ffmpeg cutting pattern
already exists (`frames.py:47-54`) — cut `t_s ± 1.5 s` as small h264 clips (optional
drawtext of the flag code) for high-severity flags, embed as `<video loop muted>` in the
HTML report.

### 4.9 Provenance envelope + architecture narrative *(1–2 h + 3–4 h)*
`session.json` is a naked list — no video name, model, settings, timings, or version
(`__version__` is used nowhere). Wrap it in a metadata object. Promote the AceLens mapping
table (the actual investment thesis, currently buried at the bottom of the README with
unexplained jargon — F3ED, CalTennis) into `docs/architecture.md` with a mermaid
demo-vs-production diagram: what transfers as-is (schema, prompts, guardrails, evals) vs
what swaps at scale.

---

## 5. P2 — engineering credibility (what technical diligence flags)

1. **Uninstallable off-Mac**: `mlx-vlm` is a hard dependency though the code already
   defers its import (`vlm.py:57`). Move to `[project.optional-dependencies] local = [...]`;
   core (opencv/numpy/pydantic) then installs anywhere, enabling CI and reviewer smoke-tests.
2. **Zero tests** — yet four pure surfaces need no Mac, model, or video: `detect_segments`
   on synthetic curves, `extract_json`, schema round-trip, prompt building. ~10 tests, one
   afternoon, plus a `ubuntu-latest` lint+test workflow. (This analysis found the splitter
   and think-tag bugs with exactly those tests.)
3. **Unpinned `mlx-vlm>=0.5.0`** on a fast-churning API — pin `>=0.6.5,<0.7` and note the
   verified-against version.
4. **No `.gitignore`** (`__pycache__` is already dirty; `*_courtside/` outputs and `.venv`
   would be committed), **no LICENSE** (blocks any serious evaluation; pick one and add
   `license` to pyproject), README `python3.12` vs `requires-python >=3.11` inconsistency.
5. Print-only logging; version duplicated between `pyproject.toml` and `__init__.py`.

---

## 6. Recommended sequence

**Day 1 — crash-proof the live path (§2):** per-clip recovery · stale-frame cleanup ·
`--resume`/`--from-dir` · pre-flights (ffmpeg, model cache, RAM) · streaming progress ·
think-strip · `.gitignore` + LICENSE.

**Days 2–4 — the wow layer (§4):** HTML report with flagged-moments gallery · run-stats
closing line · committed sample session · `--offline` + airplane-mode runbook · timeline
SVG · video snippets if time allows.

**Week 2 — diligence hardening (§3, §5):** tests + CI on the pure core · mlx-vlm to
optional extra + pin · strict-schema fix or removal · `t_s` validation + Python-computed
report stats · flag-code enum · bench side-by-side page · `docs/architecture.md`.

**Demo runbook:** rehearse the full pipeline the night before (that run becomes the cached
asset) · demo day: airplane mode, live-run 1–2 clips on `qwen3-vl-30b-a3b` (fastest decode)
with token streaming visible, then flip to the pre-baked full-session HTML report · keep
`--segment fixed` in your back pocket for unfamiliar footage.
