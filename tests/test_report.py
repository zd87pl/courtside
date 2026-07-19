"""Report aggregation: facts, run stats, cost line, and prompt building."""

from __future__ import annotations

import math

from courtside import report
from courtside.prompts import build_clip_prompt, build_report_prompt, limitations_footer
from courtside.schema import ClipAnalysis, Flag, Stroke, clip_json_schema


def _analyses():
    return [
        ClipAnalysis(
            start_s=10, end_s=18, confidence="high", rally_summary="r",
            strokes=[
                Stroke(t_s=11, player="near", stroke="forehand",
                       technique_flags=[Flag(code="late_preparation", severity="high", evidence="e")]),
                Stroke(t_s=13, player="far", stroke="backhand",
                       tactical_flags=[Flag(code="short_ball_no_approach", severity="medium", evidence="e")]),
            ],
        ),
        ClipAnalysis(
            start_s=25, end_s=31, confidence="medium", rally_summary="r",
            strokes=[Stroke(t_s=26, player="far", stroke="serve",
                            technique_flags=[Flag(code="late_prep", severity="high", evidence="e")])],
        ),
    ]


def test_facts_counts_and_synonym_merge():
    f = report.compute_session_facts(_analyses())
    assert f["total_strokes"] == 3
    assert f["player_split"] == {"near": 1, "far": 2, "unknown": 0}
    # late_preparation and its synonym late_prep must aggregate together
    assert f["top_technique_flags"]["late_preparation"] == 2


def test_run_stats_and_cost_line():
    per_clip = [
        {"wall_s": 12.0, "prompt_tokens": 14000, "generation_tokens": 300, "peak_gb": 41.2},
        {"wall_s": 9.0, "prompt_tokens": 13000, "generation_tokens": 250, "peak_gb": 40.8},
    ]
    rs = report.aggregate_run_stats(per_clip, video_duration_s=120.0, segment_s=3.0,
                                    clips_ok=2, clips_failed=1)
    assert rs["total_tokens"] == 27550
    assert rs["clips_failed"] == 1
    assert rs["peak_gb"] == 41.2
    assert rs["on_device_usd"] == 0.0 and rs["bytes_uploaded"] == 0
    line = report.cost_summary_line(rs)
    assert "realtime" in line and "bytes uploaded" in line


def test_frame_resolution_uses_coarsest_clip():
    assert math.isclose(report.frame_resolution_s([4.0, 0.71]), 1 / 0.71)
    assert report.frame_resolution_s([]) > 0


def test_limitations_footer_reflects_resolution():
    assert "~1.41s" in limitations_footer(1.41)
    assert "0.25" not in limitations_footer(1.41)  # no longer hardcoded


def test_clip_prompt_has_frame_timestamps_and_bounds():
    p = build_clip_prompt(n_frames=3, start_s=10.0, end_s=18.0,
                          schema=clip_json_schema(), timestamps=[10.0, 12.5, 15.0],
                          flag_vocab="late_preparation")
    assert "frame 1: 10.00s" in p and "frame 3: 15.00s" in p
    assert "10.00 and 18.00" in p  # explicit t_s bounds


def test_report_prompt_includes_precomputed_facts():
    facts = {"total_strokes": 3}
    p = build_report_prompt([{"x": 1}], facts)
    assert "Pre-computed session facts" in p and "\"total_strokes\": 3" in p
