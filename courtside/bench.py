"""courtside-bench - compare local models on one clip of your own footage.

Runs the identical clip-analysis prompt against each model and reports load
time, prefill tok/s (where the M5 Max neural accelerators matter), decode
tok/s (bandwidth-bound: ~614 GB/s / bytes-per-active-param is the ceiling),
and peak unified-memory use.

Example:
  courtside-bench match.mp4 --models qwen3-vl-8b qwen3-vl-30b-a3b qwen3-vl-32b
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import config
from .frames import extract_clip_frames
from .prompts import SYSTEM, build_clip_prompt
from .schema import clip_json_schema
from .segment import detect_segments, fixed_windows
from .vlm import LocalVLM


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
    p.add_argument("--kv-bits", type=float, default=None)
    args = p.parse_args(argv)

    segs = detect_segments(args.video) or fixed_windows(args.video, 15.0)
    seg = max(segs, key=lambda s: s.duration)  # busiest-looking clip
    with tempfile.TemporaryDirectory() as td:
        cf = extract_clip_frames(args.video, seg, Path(td),
                                 fps=args.fps, max_frames=args.max_frames, max_side=args.max_side)
        prompt = SYSTEM + "\n\n" + build_clip_prompt(
            len(cf.frames), cf.fps_used, seg.start_s, seg.end_s, clip_json_schema()
        )
        print(f"clip {seg.start_s:.1f}-{seg.end_s:.1f}s, {len(cf.frames)} frames @ {args.max_side}px\n")
        rows = []
        for key in args.models:
            repo = config.resolve_model(key)
            print(f"== {key} -> {repo}")
            try:
                vlm = LocalVLM(repo, kv_bits=args.kv_bits)
                text, s = vlm.generate(prompt, images=cf.frames,
                                       max_tokens=args.max_tokens, temperature=0.0)
                vlm.close()
                rows.append((key, s.load_s, s.prompt_tokens, s.prompt_tps, s.generation_tps, s.peak_gb, s.wall_s))
                print(f"   load {s.load_s:6.1f}s | prompt {s.prompt_tokens or '?':>6} tok "
                      f"@ {s.prompt_tps or 0:7.1f} t/s | decode {s.generation_tps or 0:6.1f} t/s "
                      f"| peak {s.peak_gb or 0:6.1f} GB | wall {s.wall_s:6.1f}s")
            except Exception as e:  # keep benching the rest
                print(f"   FAILED: {e}")

        if rows:
            print("\nmodel                 load_s  prompt_tok  prefill_tps  decode_tps  peak_gb  wall_s")
            for r in rows:
                print(f"{r[0]:<20}{r[1]:>7.1f}{(r[2] or 0):>11}{(r[3] or 0):>12.1f}"
                      f"{(r[4] or 0):>11.1f}{(r[5] or 0):>8.1f}{r[6]:>8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
