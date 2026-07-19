# Sample output

This directory is a **complete courtside run you can inspect without a Mac, a GPU, or a
model download** — the exact artifacts the pipeline writes:

| file | what it is |
|---|---|
| [`report.html`](demo_session/report.html) | the self-contained visual report (open it in a browser) |
| `session_report.md` | the Markdown coaching report |
| `session.json` | provenance + run metrics + every clip's structured analysis |
| `clip_000.json` … | per-clip schema-validated stroke JSON |
| `clip_000/` … | the sampled keyframes for each clip |

Open `demo_session/report.html` to see the flagged-moments gallery, the rally/stroke
timeline, per-rally cards, and the on-device cost line.

## Provenance (read this)

The **footage here is synthetic** — a plain green "court" generated with ffmpeg — and the
stroke analyses are **hand-authored to be representative**, not the output of a live model
run. It exists to show the *shape* of the output (schema, report structure, HTML layout,
the limitations guardrail) so a technical reviewer can evaluate quality without running
anything. The numbers in `session.json` are internally consistent (10 strokes, 5 near / 5
far, the cost line derives from the token totals) but are illustrative.

To produce a real run on your own footage:

```bash
courtside your_match.mp4            # writes your_match_courtside/report.html
courtside --from-dir your_match_courtside   # re-render the report later, no model needed
```
