"""Prompts for the two VLM passes: clip stroke extraction and session report.

Guardrails follow the CalTennis findings: monocular video supports joint
angles / 2D positioning / timing observations, but NOT absolute depth, foot
contact, weight transfer, or force claims. The prompts forbid those, and
report.py appends a static limitations section regardless of model output.
"""

from __future__ import annotations

import json

SYSTEM = (
    "You are an expert tennis coach and video analyst. You are precise, "
    "conservative, and you never invent details that are not visible in the "
    "provided frames. When unsure, you say 'unknown' or lower your confidence."
)

CLIP_TEMPLATE = """You are given {n_frames} frames sampled at ~{fps:.1f} fps from a tennis video clip.
The clip covers {start_s:.1f}s to {end_s:.1f}s of the full video. Frame i (1-indexed)
corresponds approximately to time start_s + (i-1)/fps.

Task: identify each visible stroke (ball contact) by either player and assess it.

Rules:
- "near" = player closer to the camera, "far" = player on the other side of the net.
- Only report strokes you can actually see. Do not guess strokes that happen off-camera
  or between frames. It is fine to report fewer strokes than actually occurred.
- t_s must be the absolute time in the FULL video (use the frame-to-time mapping above).
- technique_flags: visible form issues only - preparation timing, contact point relative
  to body (in the image plane), swing path shape, balance/recovery, follow-through,
  ball toss height/placement on serves, split-step presence, footwork spacing.
- tactical_flags: shot selection and positioning patterns visible in this clip - e.g.
  short_ball_no_approach, backhand_corner_camped, no_depth_variation, serve_placement_predictable,
  passive_mid_rally, poor_recovery_position.
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

Session clip analyses (JSON):
{clips_json}

Write the report in Markdown with exactly these sections:

# Session Analysis
## Overview
2-4 sentences: what was practiced/played, stroke mix, overall impression. Include total
strokes analyzed and the near/far split, computed from the JSON.

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
unknown strokes).

Style: direct, specific, coach-to-coach. No filler praise. Do not mention that you are an
AI. Do not add sections beyond the five above.
"""


def build_clip_prompt(n_frames: int, fps: float, start_s: float, end_s: float, schema: dict) -> str:
    return CLIP_TEMPLATE.format(
        n_frames=n_frames,
        fps=fps,
        start_s=start_s,
        end_s=end_s,
        schema=json.dumps(schema, indent=None),
    )


def build_report_prompt(clips: list[dict]) -> str:
    return REPORT_TEMPLATE.format(clips_json=json.dumps(clips, indent=None))


LIMITATIONS_FOOTER = """
---
## Limitations (auto-generated)
This report is derived from single-camera video. It can support observations about
timing, swing shape in the image plane, court positioning, and shot patterns. It cannot
reliably measure absolute depth or distances, foot-contact timing, weight transfer, or
forces - treat any such claims as out of scope. Stroke timestamps are approximate
(frame-sampling resolution ~0.25s). Some strokes may be missed between sampled frames;
counts are lower bounds, not exact totals.
"""
