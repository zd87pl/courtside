"""courtside CLI - segment a session video, analyze clips with a local VLM,
and write a coaching report.

Examples
--------
# quick plumbing check, no model load:
courtside match.mp4 --dry-run

# default run (Qwen3-VL-32B 8-bit, auto rally detection):
courtside match.mp4

# fast smoke test on the first 3 clips with the 8B model:
courtside match.mp4 --model qwen3-vl-8b --max-clips 3

# flagship stretch config on 128GB (see README for wired-limit sysctl):
courtside match.mp4 --model qwen3-vl-235b --kv-bits 4 --max-frames 24

# against a running server (mlx_vlm.server / LM Studio), with strict schema:
courtside match.mp4 --server-url http://localhost:8080/v1 --server-model Qwen/Qwen3-VL-32B-Instruct
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from . import config
from .frames import extract_clip_frames, probe_duration
from .prompts import LIMITATIONS_FOOTER, SYSTEM, build_clip_prompt, build_report_prompt
from .schema import ClipAnalysis, clip_json_schema
from .segment import Segment, detect_segments, fixed_windows, from_file
from .vlm import LocalVLM, ServerVLM, extract_json


def _log(msg: str) -> None:
    print(msg, flush=True)


def analyze_clip(vlm, clip_frames, schema: dict, max_tokens: int) -> tuple[ClipAnalysis, dict]:
    seg = clip_frames.segment
    prompt = SYSTEM + "\n\n" + build_clip_prompt(
        n_frames=len(clip_frames.frames),
        fps=clip_frames.fps_used,
        start_s=seg.start_s,
        end_s=seg.end_s,
        schema=schema,
    )
    text, stats = vlm.generate(
        prompt, images=clip_frames.frames, max_tokens=max_tokens,
        temperature=0.0, json_schema=schema,
    )
    try:
        parsed = ClipAnalysis.model_validate(extract_json(text))
    except (ValueError, ValidationError) as e:
        # one repair round: feed the error back, no images needed
        _log(f"    ! invalid JSON ({type(e).__name__}), retrying once")
        repair = (
            "Your previous output was invalid JSON for the required schema.\n"
            f"Error: {e}\n\nPrevious output:\n{text}\n\n"
            "Return ONLY the corrected JSON object, nothing else."
        )
        text2, stats2 = vlm.generate(repair, images=None, max_tokens=max_tokens,
                                     temperature=0.0, json_schema=schema)
        parsed = ClipAnalysis.model_validate(extract_json(text2))
        stats.wall_s += stats2.wall_s
    # trust the pipeline's clip boundaries over the model's
    parsed.start_s, parsed.end_s = seg.start_s, seg.end_s
    return parsed, {
        "wall_s": round(stats.wall_s, 2),
        "prompt_tokens": stats.prompt_tokens,
        "prompt_tps": round(stats.prompt_tps, 1) if stats.prompt_tps else None,
        "generation_tps": round(stats.generation_tps, 1) if stats.generation_tps else None,
        "peak_gb": round(stats.peak_gb, 2) if stats.peak_gb else None,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="courtside", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--model", default=config.DEFAULT_MODEL_KEY,
                   help=f"registry key or HF repo/local path (default: {config.DEFAULT_MODEL_KEY}; "
                        f"keys: {', '.join(config.MODELS)})")
    p.add_argument("--segment", choices=["auto", "fixed", "file"], default="auto")
    p.add_argument("--window", type=float, default=20.0, help="window seconds for --segment fixed")
    p.add_argument("--segments-file", type=Path, help="JSON segments for --segment file")
    p.add_argument("--fps", type=float, default=config.DEFAULT_FPS)
    p.add_argument("--max-frames", type=int, default=config.DEFAULT_MAX_FRAMES)
    p.add_argument("--max-side", type=int, default=config.DEFAULT_MAX_SIDE)
    p.add_argument("--max-clips", type=int, default=0, help="limit clips (0 = all)")
    p.add_argument("--max-tokens", type=int, default=1400)
    p.add_argument("--kv-bits", type=float, default=None, help="KV cache quantization bits (e.g. 4 or 8)")
    p.add_argument("--out", type=Path, default=None, help="output dir (default: <video>_courtside)")
    p.add_argument("--dry-run", action="store_true", help="segment + extract frames only, no model")
    p.add_argument("--server-url", default=None, help="OpenAI-compatible base URL instead of local load")
    p.add_argument("--server-model", default=None, help="model name for --server-url")
    args = p.parse_args(argv)

    video: Path = args.video
    if not video.exists():
        p.error(f"video not found: {video}")
    out_dir = args.out or video.with_name(video.stem + "_courtside")
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(video)
    _log(f"video: {video.name}  ({duration/60:.1f} min)")

    # 1) segment
    t0 = time.perf_counter()
    if args.segment == "auto":
        segments = detect_segments(video, min_rally_s=config.DEFAULT_MIN_RALLY_S,
                                   max_clip_s=config.DEFAULT_MAX_CLIP_S)
    elif args.segment == "fixed":
        segments = fixed_windows(video, args.window)
    else:
        if not args.segments_file:
            p.error("--segment file requires --segments-file")
        segments = from_file(args.segments_file)
    if args.max_clips:
        segments = segments[: args.max_clips]
    _log(f"segments: {len(segments)} active clips "
         f"({sum(s.duration for s in segments):.0f}s of play, {time.perf_counter()-t0:.1f}s to detect)")
    if not segments:
        _log("no active segments found - try --segment fixed")
        return 1

    # 2) frames
    clips = []
    for i, seg in enumerate(segments):
        cf = extract_clip_frames(video, seg, out_dir / f"clip_{i:03d}",
                                 fps=args.fps, max_frames=args.max_frames, max_side=args.max_side)
        clips.append(cf)
        _log(f"  clip {i:03d}  {seg.start_s:7.1f}-{seg.end_s:7.1f}s  {len(cf.frames)} frames @ {cf.fps_used:.2f} fps")

    if args.dry_run:
        _log("dry run complete - frames are on disk, no model loaded")
        return 0

    # 3) VLM
    if args.server_url:
        if not args.server_model:
            p.error("--server-url requires --server-model")
        vlm = ServerVLM(args.server_url, args.server_model)
        _log(f"backend: server {args.server_url}  model={args.server_model}")
    else:
        repo = config.resolve_model(args.model)
        _log(f"backend: local mlx-vlm  model={repo}  (loading...)")
        vlm = LocalVLM(repo, kv_bits=args.kv_bits)
        _log(f"loaded in {vlm.load_s:.1f}s")

    schema = clip_json_schema()
    analyses: list[ClipAnalysis] = []
    try:
        for i, cf in enumerate(clips):
            _log(f"[{i+1}/{len(clips)}] analyzing clip {cf.segment.start_s:.1f}-{cf.segment.end_s:.1f}s ...")
            analysis, stats = analyze_clip(vlm, cf, schema, args.max_tokens)
            analyses.append(analysis)
            (out_dir / f"clip_{i:03d}.json").write_text(analysis.model_dump_json(indent=2))
            n_flags = sum(len(s.technique_flags) + len(s.tactical_flags) for s in analysis.strokes)
            _log(f"    {len(analysis.strokes)} strokes, {n_flags} flags, conf={analysis.confidence}  "
                 f"({stats['wall_s']}s, prefill {stats['prompt_tps'] or '?'} t/s, "
                 f"decode {stats['generation_tps'] or '?'} t/s, peak {stats['peak_gb'] or '?'} GB)")

        # 4) report (text-only pass on the same model)
        session = [a.model_dump() for a in analyses]
        (out_dir / "session.json").write_text(json.dumps(session, indent=2))
        _log("generating session report ...")
        report_text, rstats = vlm.generate(
            SYSTEM + "\n\n" + build_report_prompt(session),
            images=None, max_tokens=2400, temperature=0.4,
        )
        report_path = out_dir / "session_report.md"
        report_path.write_text(report_text.strip() + "\n" + LIMITATIONS_FOOTER)
        _log(f"report written: {report_path}  ({rstats.wall_s:.1f}s)")
    finally:
        vlm.close()

    total_strokes = sum(len(a.strokes) for a in analyses)
    _log(f"done: {len(analyses)} clips, {total_strokes} strokes -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
