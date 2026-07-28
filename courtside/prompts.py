"""Prompts for the two VLM passes: clip stroke extraction and session report.

Guardrails follow the CalTennis findings: monocular video supports joint
angles / 2D positioning / timing observations, but NOT absolute depth, foot
contact, weight transfer, or force claims. The prompts forbid those, and
analyze.py appends a static limitations section regardless of model output.
"""

from __future__ import annotations

import json

SYSTEM = (
    "You are an expert tennis coach and video analyst. You are precise, "
    "conservative, and you never invent details that are not visible in the "
    "provided frames. When unsure, you say 'unknown' or lower your confidence."
)

CLIP_TEMPLATE = """You are given {n_frames} frames sampled from a tennis video clip.
The clip covers {start_s:.2f}s to {end_s:.2f}s of the full video. Each frame's absolute
timestamp in the full video is listed below:
{frame_times}

Task: identify each visible stroke (ball contact) by either player and assess it.

Rules:
- "near" = player closer to the camera, "far" = player on the other side of the net.
- The first stroke a player hits immediately after the opponent's serve is a "return" -
  label it "return", not forehand/backhand.
- Only report strokes you can actually see. Do not guess strokes that happen off-camera
  or between frames. It is fine to report fewer strokes than actually occurred.
- t_s must be the absolute time in the FULL video. Use the per-frame timestamps above:
  set t_s to the timestamp of the frame where contact is clearest. t_s must fall between
  {start_s:.2f} and {end_s:.2f}.
- technique_flags: visible form issues only - preparation timing, contact point relative
  to body (in the image plane), swing path shape, balance/recovery, follow-through,
  ball toss height/placement on serves, split-step presence, footwork spacing.
- tactical_flags: shot selection and positioning patterns visible in this clip - e.g.
  short_ball_no_approach, backhand_corner_camped, no_depth_variation, serve_placement_predictable,
  passive_mid_rally, poor_recovery_position.
- Prefer these canonical flag codes where they apply (snake_case): {flag_vocab}.
- FORBIDDEN (monocular video cannot support these): claims about ground reaction force,
  weight transfer percentages, exact depth/distance in meters, foot-contact timing,
  racquet-head speed numbers. Never output these.
- Use severity: low = stylistic, medium = costs points sometimes, high = recurring point-loser.
- If the clip contains no tennis strokes (changeover, walking, ball pickup), return an
  empty strokes list, set confidence to "high", and say so in rally_summary.

Return ONLY a JSON object matching this schema (no markdown fences, no commentary):
{schema}
"""

REPORT_TEMPLATE = """You are writing a post-session coaching report for a tennis coach, based on
structured per-clip analyses produced from video. The JSON below is the ONLY source of
truth. Do not invent strokes, flags, or numbers that are not present in it.

Pre-computed session facts (authoritative - use these exact numbers, do not recount):
{facts}

Session clip analyses (JSON):
{clips_json}

Write the report in Markdown with exactly these sections:

# Session Analysis
## Overview
2-4 sentences: what was practiced/played, stroke mix, overall impression. Use the
pre-computed total stroke count and near/far split above verbatim.

## Technique Themes
The 3-5 most recurrent technique_flags across clips. For each: what it is, why it matters,
and cite the supporting clips/timestamps like (t=123.4s). Only cite timestamps that exist
in the JSON.

## Tactical Patterns
The 2-4 most recurrent tactical_flags, same citation rules.

## Prioritized Drills
Exactly 3 drills, ordered by expected impact. Each: name, setup, success criterion, and
which flagged issue it targets.

## Confidence Notes
1-3 sentences on where the analysis is least certain (low-confidence clips, occlusions,
unknown strokes, skipped clips).

Style: direct, specific, coach-to-coach. No filler praise. Do not mention that you are an
AI. Do not add sections beyond the five above.
"""


def _frame_times_block(timestamps: list[float]) -> str:
    return "\n".join(f"  frame {i + 1}: {t:.2f}s" for i, t in enumerate(timestamps))


def build_clip_prompt(
    n_frames: int,
    start_s: float,
    end_s: float,
    schema: dict,
    timestamps: list[float] | None = None,
    flag_vocab: str = "",
) -> str:
    if timestamps is None:
        timestamps = []
    return CLIP_TEMPLATE.format(
        n_frames=n_frames,
        start_s=start_s,
        end_s=end_s,
        frame_times=_frame_times_block(timestamps),
        flag_vocab=flag_vocab,
        schema=json.dumps(schema, indent=None),
    )


def build_report_prompt(clips: list[dict], facts: dict | None = None) -> str:
    return REPORT_TEMPLATE.format(
        facts=json.dumps(facts or {}, indent=2),
        clips_json=json.dumps(clips, indent=None),
    )


def limitations_footer(frame_resolution_s: float | None = None) -> str:
    """Static limitations section; the sampling resolution is parametrized so it
    stays truthful when --fps changes or clips are capped (finding: footer
    hardcoded ~0.25s)."""
    res = (
        f"~{frame_resolution_s:.2f}s"
        if frame_resolution_s and frame_resolution_s > 0
        else "the frame-sampling interval"
    )
    return f"""
---
## Limitations (auto-generated)
This report is derived from single-camera video. It can support observations about
timing, swing shape in the image plane, court positioning, and shot patterns. It cannot
reliably measure absolute depth or distances, foot-contact timing, weight transfer, or
forces - treat any such claims as out of scope. Stroke timestamps are approximate
(frame-sampling resolution {res}). Some strokes may be missed between sampled frames;
counts are lower bounds, not exact totals.
"""


# Back-compat constant for any external importer; uses the default 4fps interval.
LIMITATIONS_FOOTER = limitations_footer(0.25)
