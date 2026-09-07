# Local pipeline and UI

This guide covers the Python CLI and local dashboard. To integrate Courtside into
an app, start with the [API quick start](../README.md#local-api-quick-start) instead.
The CLI runs without the API, Postgres, or S3.

You can run inference on Apple Silicon with MLX, or call an OpenAI-compatible
model endpoint from another computer. The local MLX default is `qwen3-vl-32b`;
the API/OpenRouter default is `qwen/qwen3.8-27b`.

## Setup

From the repository root:

```bash
./setup.sh
```

The interactive script detects the platform and RAM, sets up dependencies, offers
model selection and prefetching, then starts a sample demo or a run on your video.
The [sample report](../examples/demo_session/report.html) uses synthetic footage
and illustrative analyses; see its [provenance](../examples/README.md#provenance-read-this).

For a manual installation on Apple Silicon, use Python 3.11+:

```bash
brew install ffmpeg
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[local]'
```

For OpenRouter or another model server, install `'.[server]'` in place of
`'.[local]'`. The core package's segmentation/report tools work without MLX.
On Linux, install ffmpeg through your distribution's package manager.

Optional extras:

```bash
python -m pip install -e '.[youtube]'  # URL downloads using yt-dlp
python -m pip install -e '.[pose]'     # pose overlays and angle estimates
```

The first local-model run downloads Hugging Face weights. Set `HF_HOME` before
starting if you need a different cache location. Model weights require substantial
disk and runtime memory; check the registry in [config.py](../courtside/config.py).

## Run analysis

After activating the virtual environment:

```bash
# Segmentation and frame extraction only; no model calls.
courtside match.mp4 --dry-run

# Local MLX preview on Apple Silicon.
courtside match.mp4 --model qwen3-vl-8b --max-clips 3 --moments 0 --no-pose

# Full run using the local default model and options.
courtside match.mp4

# Re-render an existing session without calling a model.
courtside --from-dir match_courtside

# Resume local analysis, skipping clips already written.
courtside match.mp4 --resume
```

The CLI's resume option applies to its local output directory. API worker retries
currently restart analysis; they do not use this checkpoint mechanism.

With the `youtube` extra installed, a supported video URL can replace the file
path. The source is downloaded before processing; only use footage you may process.
Run `courtside --help` for the full command reference.

## OpenRouter

Install the `server` extra, then run in Bash:

```bash
read -r -s -p 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
export OPENROUTER_API_KEY
courtside match.mp4 --server-url https://openrouter.ai/api/v1 \
  --server-model qwen/qwen3.8-27b --max-clips 3 --moments 0 --no-pose
```

This path sends sampled frames and prompts to OpenRouter and uses your provider
credits. It does not require local Qwen weights. For this model, the client requests
non-thinking mode to preserve its output budget; see [model details](../api/README.md#default-llm).
The endpoint is explicit: the CLI does not automatically fall back from local
inference to a hosted provider.

For a local OpenAI-compatible model server, supply its URL and loaded model ID:

```bash
courtside match.mp4 --server-url http://localhost:8080/v1 \
  --server-model your-loaded-vision-model --max-clips 3
```

The server must already be running and support image inputs. Port 8080 is also the
local Courtside API's default port; use a different port if running both services.
The client requests JSON-schema output, with validation/repair and a compatibility
fallback for providers that reject the schema.

## Local dashboard

```bash
courtside-ui
# Or select the directories to scan:
courtside-ui --root ~/tennis-videos
```

Open [the dashboard](http://127.0.0.1:8799). It binds to loopback and runs one
analysis at a time. You can choose a video, inspect progress/logs, cancel work,
and open or export a session report.

Local MLX analysis keeps frames on the computer after models are cached. Selecting
**use a cloud model via OpenRouter** sends frames to the hosted provider and
requires `OPENROUTER_API_KEY` in the dashboard process. URL inputs also use the
network to download the source. The report labels the processing mode.

This dashboard is separate from the Next.js `cloud/` prototype and the mobile API;
it does not display API jobs or share their account keys.

## Outputs and optional moments

Outputs are written under `<video>_courtside/`, or the directory supplied by `--out`:

| Artifact | Contents |
|---|---|
| `clip_NNN/`, `clip_NNN.json` | Sampled frames and per-clip analysis/status |
| `session.json` | Model provenance, metrics, clip analyses, and aggregate facts |
| `session_report.md` | Generated Markdown report |
| `report.html` | Self-contained report with embedded evidence |
| `moments/` | Optional short clips, overlays, and moment assets |

By default, up to six flagged moments receive short clips and focused model
analysis. The `pose` extra adds skeleton overlays, contact-frame refinement,
joint-angle estimates, and comparisons with other strokes in the session.
`--moments 0` disables moment generation; `--no-pose` skips pose measurements.
`--smooth-slowmo` enables more expensive motion-interpolated clips.

Pose angles are 2D image-plane estimates. Missing pose dependencies or individual
moment failures can leave results without those assets. The first local pose run
may download weights; the deployed API image bundles its pose model. Review
[third-party licensing](third-party.md) when using these components.

## Offline local runs

Cache the selected MLX model while online, then use a local video:

```bash
courtside --prefetch qwen3-vl-8b
courtside match.mp4 --model qwen3-vl-8b --offline --moments 0 --no-pose
```

`--offline` sets Hugging Face/Transformers offline flags and rejects URL downloads.
It is not an operating-system network sandbox. Use it with the local MLX backend;
do not combine it with `--server-url`. Pre-cache any optional models you enable,
and disable network access separately if your workflow requires isolation.

## Tuning and troubleshooting

| Option | Effect |
|---|---|
| `--max-clips 3` | Limit the preview to three segments; `0` means all |
| `--fps 4 --max-frames 32` | Default sampling target/cap; long clips reduce sampling density |
| `--max-side 672` | Reduce frame resolution and vision input size; may lose detail |
| `--segment fixed --window 20` | Use fixed windows when motion segmentation is unsuitable |
| `--segment file --segments-file segs.json` | Use a JSON list of `[start_s, end_s]` pairs |
| `--from-ts 00:30 --to-ts 01:30` | Restrict analysis to a portion of the video |
| `--kv-bits 4` | Quantize the local MLX KV cache; memory/quality tradeoff |
| `--stream` | Print tokens during local generation |

For local MLX generation failures, run `courtside-doctor` or
`courtside-doctor --video match.mp4`. It exercises different frame counts and the
analysis prompt to help distinguish model/dependency failures from input limits.
For hosted inference, check credentials, model image support, and provider errors.

Use `courtside-bench match.mp4 --models qwen3-vl-8b qwen3-vl-32b --out bench_out`
to retain local-model outputs for comparison on your footage. Results vary with
camera position, sampling, and model; no fixed throughput or accuracy is promised.
