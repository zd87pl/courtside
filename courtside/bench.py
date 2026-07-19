"""courtside-bench - compare local models on one clip of your own footage.

Runs the identical clip-analysis prompt against each model and reports load
time, prefill tok/s (where the M5 Max neural accelerators matter), decode
tok/s (bandwidth-bound: ~614 GB/s / bytes-per-active-param is the ceiling),
and peak unified-memory use. With --out it also persists each model's parsed
analysis so you can show the qualitative divergence (the 8B missing what the
32B catches), not just the speed numbers.

Example:
  courtside-bench match.mp4 --models qwen3-vl-8b qwen3-vl-30b-a3b qwen3-vl-32b --out bench_out
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import config
from .frames import extract_clip_frames, require_tools
from .prompts import SYSTEM, build_clip_prompt
from .schema import CANONICAL_FLAG_CODES, ClipAnalysis, clip_json_schema
from .segment import detect_segments, fixed_windows
from .vlm import LocalVLM, extract_json

_FLAG_VOCAB = ", ".join(sorted(CANONICAL_FLAG_CODES))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="courtside-bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path)
    p.add_argument("--models", nargs="+", default=["qwen3-vl-8b", "qwen3-vl-32b"],
                   help=f"registry keys or repos (keys: {', '.join(config.MODELS)})")
    p.add_argument("--fps", type=float, default=config.DEFAULT_FPS)
    p.add_argument("--max-frames", type=int, default=config.DEFAULT_MAX_FRAMES)
    p.add_argument("--max-side", type=int, default=config.DEFAULT_MAX_SIDE)
    p.add_argument("--max-tokens", type=int, default=600)
    p.add_argument("--kv-bits", type=int, default=None)
    p.add_argument("--out", type=Path, default=None, help="persist frames + per-model outputs here")
    args = p.parse_args(argv)

    require_tools()

    args.video = args.video.resolve()  # leading-dash filenames vs ffmpeg/cv2
    segs = detect_segments(args.video) or fixed_windows(args.video, 15.0)
    if not segs:
        print("no segments found in video - is it readable?")
        return 1
    seg = max(segs, key=lambda s: s.duration)  # busiest-looking clip

    frames_root = args.out if args.out else Path(tempfile.mkdtemp())
    frames_dir = frames_root / "clip"
    cf = extract_clip_frames(args.video, seg, frames_dir,
                             fps=args.fps, max_frames=args.max_frames, max_side=args.max_side)
    prompt = SYSTEM + "\n\n" + build_clip_prompt(
        len(cf.frames), seg.start_s, seg.end_s, clip_json_schema(),
        timestamps=cf.timestamps(), flag_vocab=_FLAG_VOCAB,
    )
    print(f"clip {seg.start_s:.1f}-{seg.end_s:.1f}s, {len(cf.frames)} frames @ {args.max_side}px\n")
    rows = []
    outputs: dict[str, dict] = {}
    for key in args.models:
        repo = config.resolve_model(key)
        print(f"== {key} -> {repo}")
        vlm = None
        try:
            vlm = LocalVLM(repo, kv_bits=args.kv_bits)
            text, s = vlm.generate(prompt, images=cf.frames,
                                   max_tokens=args.max_tokens, temperature=0.0)
            rows.append((key, s.load_s, s.prompt_tokens, s.prompt_tps, s.generation_tps, s.peak_gb, s.wall_s))
            # try to parse for the qualitative comparison / validity column
            valid, n_strokes, n_flags = False, 0, 0
            try:
                a = ClipAnalysis.model_validate(extract_json(text))
                valid, n_strokes = True, len(a.strokes)
                n_flags = sum(len(st.technique_flags) + len(st.tactical_flags) for st in a.strokes)
                outputs[key] = {"repo": repo, "valid": True, "analysis": a.model_dump()}
            except Exception:
                outputs[key] = {"repo": repo, "valid": False, "raw": text[:2000]}
            print(f"   load {s.load_s:6.1f}s | prompt {s.prompt_tokens or '?':>6} tok "
                  f"@ {s.prompt_tps or 0:7.1f} t/s | decode {s.generation_tps or 0:6.1f} t/s "
                  f"| peak {s.peak_gb or 0:6.1f} GB | wall {s.wall_s:6.1f}s "
                  f"| {'valid' if valid else 'INVALID'} {n_strokes} strokes/{n_flags} flags")
        except Exception as e:  # keep benching the rest
            print(f"   FAILED: {e}")
        finally:
            if vlm is not None:  # always release before loading the next model
                vlm.close()

    if rows:
        print("\nmodel                 load_s  prompt_tok  prefill_tps  decode_tps  peak_gb  wall_s")
        for r in rows:
            print(f"{r[0]:<20}{r[1]:>7.1f}{(r[2] or 0):>11}{(r[3] or 0):>12.1f}"
                  f"{(r[4] or 0):>11.1f}{(r[5] or 0):>8.1f}{r[6]:>8.1f}")

    if args.out and outputs:
        (args.out / "bench.json").write_text(json.dumps(outputs, indent=2))
        print(f"\nper-model outputs written to {args.out / 'bench.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
