"""Pure-Python aggregation for the session report and session.json envelope.

Everything an investor asks about ("how many strokes?", "cost vs cloud?",
"how long per hour of footage?") is computed here from data the pipeline
already collects, rather than asked of the LLM (finding: report outsourced
arithmetic to the model; run metrics were computed then discarded).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import config
from .schema import ClipAnalysis, normalize_flag_code


def compute_session_facts(analyses: list[ClipAnalysis]) -> dict[str, Any]:
    """Authoritative counts the report prompt is told to reuse verbatim."""
    total_strokes = sum(len(a.strokes) for a in analyses)
    near = sum(1 for a in analyses for s in a.strokes if s.player == "near")
    far = sum(1 for a in analyses for s in a.strokes if s.player == "far")
    unknown = total_strokes - near - far

    stroke_mix: Counter[str] = Counter()
    tech: Counter[str] = Counter()
    tact: Counter[str] = Counter()
    for a in analyses:
        for s in a.strokes:
            stroke_mix[s.stroke] += 1
            for f in s.technique_flags:
                tech[normalize_flag_code(f.code)] += 1
            for f in s.tactical_flags:
                tact[normalize_flag_code(f.code)] += 1

    conf = Counter(a.confidence for a in analyses)
    return {
        "clips_analyzed": len(analyses),
        "total_strokes": total_strokes,
        "player_split": {"near": near, "far": far, "unknown": unknown},
        "stroke_mix": dict(stroke_mix.most_common()),
        "top_technique_flags": dict(tech.most_common(8)),
        "top_tactical_flags": dict(tact.most_common(8)),
        "confidence_distribution": dict(conf),
    }


def frame_resolution_s(fps_used_values: list[float]) -> float:
    """Worst-case (coarsest) sampling interval across clips, for the footer."""
    usable = [f for f in fps_used_values if f and f > 0]
    if not usable:
        return 1.0 / config.DEFAULT_FPS
    return max(1.0 / f for f in usable)


def aggregate_run_stats(
    per_clip_stats: list[dict[str, Any]],
    video_duration_s: float,
    segment_s: float,
    clips_ok: int,
    clips_failed: int,
    total_wall_s: float | None = None,
    timings: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Roll per-clip timing/token stats into the investor-facing summary line.

    ``total_wall_s`` is the true end-to-end elapsed time (segmentation + frame
    extraction + model load + inference + report generation). Pass it so the
    headline "processed X in Y" and the realtime factor are honest wall-clock,
    not just summed inference time (which excludes load/extract/report). When
    omitted it falls back to inference + segmentation.
    """
    def _sum(key: str) -> float:
        return sum((s.get(key) or 0) for s in per_clip_stats)

    inference_s = _sum("wall_s")
    total_wall = total_wall_s if total_wall_s and total_wall_s > 0 else inference_s + segment_s
    prompt_tokens = int(_sum("prompt_tokens"))
    gen_tokens = int(_sum("generation_tokens"))
    total_tokens = prompt_tokens + gen_tokens
    peak_gb = max((s.get("peak_gb") or 0) for s in per_clip_stats) if per_clip_stats else 0.0

    realtime_factor = (video_duration_s / total_wall) if total_wall > 0 else None
    cloud_equiv = total_tokens / 1_000_000 * config.CLOUD_INPUT_USD_PER_MTOK

    out = {
        "video_duration_s": round(video_duration_s, 1),
        "segment_s": round(segment_s, 1),
        "inference_wall_s": round(inference_s, 1),
        "total_wall_s": round(total_wall, 1),
        "realtime_factor": round(realtime_factor, 2) if realtime_factor else None,
        "prompt_tokens": prompt_tokens,
        "generation_tokens": gen_tokens,
        "total_tokens": total_tokens,
        "peak_gb": round(peak_gb, 2) if peak_gb else None,
        "cloud_equiv_usd": round(cloud_equiv, 2),
        "on_device_usd": 0.0,
        "bytes_uploaded": 0,
        "clips_ok": clips_ok,
        "clips_failed": clips_failed,
    }
    if timings:
        out["timings"] = {k: round(v, 1) for k, v in timings.items()
                          if isinstance(v, (int, float))}
    return out


def cost_summary_line(run_stats: dict[str, Any]) -> str:
    """The one line the whole demo is arguing for."""
    dur_min = run_stats["video_duration_s"] / 60
    wall_min = run_stats["total_wall_s"] / 60
    rt = run_stats.get("realtime_factor")
    rt_str = f"{rt}x realtime" if rt else "n/a"
    tok_k = run_stats["total_tokens"] / 1000
    return (
        f"Processed {dur_min:.1f} min of footage in {wall_min:.1f} min ({rt_str}) - "
        f"{tok_k:.0f}k tokens - cloud-API equivalent ~ ${run_stats['cloud_equiv_usd']:.2f}, "
        f"on-device cost ${run_stats['on_device_usd']:.2f}, {run_stats['bytes_uploaded']} bytes uploaded."
    )


def build_session_doc(
    *,
    version: str,
    video_name: str,
    video_duration_s: float,
    model: str,
    backend: str,
    settings: dict[str, Any],
    created_at: str | None,
    run_stats: dict[str, Any],
    facts: dict[str, Any],
    clips: list[dict[str, Any]],
) -> dict[str, Any]:
    """The session.json envelope: provenance + metrics + per-clip records."""
    return {
        "courtside_version": version,
        "video": video_name,
        "video_duration_s": round(video_duration_s, 1),
        "model": model,
        "backend": backend,
        "settings": settings,
        "created_at": created_at,
        "on_device": backend == "local",
        "run_stats": run_stats,
        "facts": facts,
        "clips": clips,
    }
