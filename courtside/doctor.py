"""courtside-doctor - isolate why local VLM generation fails, in one run.

Bypasses courtside's own prompt/parse machinery and drives mlx-vlm's canonical
``generate()`` path directly, stepping from trivial to realistic:

  [1] environment      python / mlx / mlx-vlm versions, RAM, GPU wired limit
  [2] model load       time + peak memory
  [3] 1 image          trivial prompt   ("describe this image")
  [4] 8 images         trivial prompt
  [5] 32 images        trivial prompt   (courtside's default frame count)
  [6] 8 images         the real courtside clip prompt

Each step reports text produced, generation_tokens, and finish_reason, so the
failure layer is unambiguous:

  - step 3 already empty with ~0 generation tokens -> the model emits EOS as
    its first token: a generation-stack problem, not a memory or prompt one.
    mlx-vlm >= 0.6.4 has a reported Qwen-family regression (Blaizzy/mlx-vlm
    issue #1526); the known-good workaround is pinning mlx-vlm==0.6.3.
  - steps 3-4 fine but 5 empty/failed -> frame-count/memory ceiling: lower
    --max-frames/--max-side or raise the GPU wired limit.
  - step 5 fine but 6 empty -> prompt-specific; send the doctor output.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import config
from .frames import extract_clip_frames, require_tools
from .prompts import SYSTEM, build_clip_prompt
from .schema import CANONICAL_FLAG_CODES, clip_json_schema
from .segment import Segment

TRIVIAL_PROMPT = "Describe what you see in one short sentence."


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _versions() -> dict:
    out: dict = {"python": platform.python_version(), "platform": platform.platform()}
    for mod in ("mlx", "mlx_vlm"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", "?")
        except ImportError:
            out[mod] = "NOT INSTALLED"
    ram = config.total_ram_gb()
    out["ram_gb"] = round(ram, 1) if ram else "?"
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["sysctl", "-n", "iogpu.wired_limit_mb"],
                               capture_output=True, text=True, timeout=5)
            out["iogpu.wired_limit_mb"] = r.stdout.strip() or "(default: ~75% of RAM)"
        except Exception:
            out["iogpu.wired_limit_mb"] = "?"
    return out


def _make_frames(video: Path | None, n: int, max_side: int, workdir: Path) -> list[Path]:
    """Real frames from the video when given, else a synthetic test frame xN."""
    if video is not None:
        cf = extract_clip_frames(video, Segment(0.0, max(20.0, n / 2)), workdir / f"clip_{n}",
                                 fps=4.0, max_frames=n, max_side=max_side)
        return cf.frames
    frame = workdir / "test.jpg"
    if not frame.exists():
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc=duration=1:size={max_side}x{max_side * 9 // 16}:rate=1",
             "-frames:v", "1", str(frame)],
            check=True)
    return [frame] * n


def _gen_step(model, processor, prompt: str, images: list[Path], max_tokens: int) -> dict:
    """One canonical mlx-vlm generate() call; never raises."""
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template

    res: dict = {"n_images": len(images), "ok": False}
    try:
        formatted = apply_chat_template(processor, model.config, prompt, num_images=len(images))
        t0 = time.perf_counter()
        out = generate(model, processor, formatted, image=[str(p) for p in images],
                       max_tokens=max_tokens, temperature=0.0)
        res.update({
            "ok": True,
            "wall_s": round(time.perf_counter() - t0, 1),
            "text": getattr(out, "text", "") or "",
            "generation_tokens": getattr(out, "generation_tokens", None),
            "prompt_tokens": getattr(out, "prompt_tokens", None),
            "finish_reason": getattr(out, "finish_reason", None),
            "peak_gb": round(getattr(out, "peak_memory", 0.0) or 0.0, 1),
        })
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc(limit=4)
    return res


def _show(label: str, r: dict) -> None:
    _p(f"\n--- {label} ---")
    if not r.get("ok"):
        _p(f"  RAISED: {r.get('error')}")
        if r.get("trace"):
            _p("  " + r["trace"].replace("\n", "\n  "))
        return
    text = (r.get("text") or "").strip()
    _p(f"  generation_tokens={r.get('generation_tokens')}  "
       f"prompt_tokens={r.get('prompt_tokens')}  finish_reason={r.get('finish_reason')}  "
       f"peak={r.get('peak_gb')}GB  wall={r.get('wall_s')}s")
    _p(f"  text: {text[:220]!r}" + (" ..." if len(text) > 220 else "") if text else "  text: <EMPTY>")


def _is_empty(r: dict) -> bool:
    return bool(r.get("ok")) and not (r.get("text") or "").strip()


def _verdict(env: dict, steps: dict[str, dict]) -> list[str]:
    """Turn step results into a specific diagnosis + remediation."""
    v: list[str] = []
    s1, s8, s32, sreal = (steps.get(k, {}) for k in ("img1", "img8", "img32", "real"))

    if _is_empty(s1) or (s1.get("ok") and (s1.get("generation_tokens") or 0) <= 1):
        v.append("DIAGNOSIS: the model emits EOS as its first token even for a trivial "
                 "single-image prompt - a generation-stack problem, not memory or prompting.")
        v.append(f"Your mlx-vlm is {env.get('mlx_vlm')}. Versions >= 0.6.4 have a reported "
                 "Qwen-family regression (Blaizzy/mlx-vlm issue #1526); 0.6.3 is known good.")
        v.append("TRY:  pip install --force-reinstall --no-deps 'mlx-vlm==0.6.3'  "
                 "then re-run courtside-doctor.")
        return v
    if not s1.get("ok"):
        v.append("DIAGNOSIS: even a single-image generate() raises - see the traceback above. "
                 "This is an mlx-vlm/model install issue, not a courtside one.")
        v.append("TRY: reinstall the stack: pip install --force-reinstall 'mlx-vlm==0.6.3' "
                 "and re-download the model after clearing its HF cache entry.")
        return v

    if s1.get("ok") and (not s32.get("ok") or _is_empty(s32)):
        if s8.get("ok") and not _is_empty(s8):
            v.append("DIAGNOSIS: generation works at 1-8 images but breaks at 32 - a "
                     "frame-count / memory ceiling on this machine for this model.")
        else:
            v.append("DIAGNOSIS: generation works with 1 image but breaks with several - "
                     "a multi-image / memory issue for this model on this machine.")
        v.append("TRY:  courtside <video> --max-frames 12 --max-side 672   (or the 8B model)")
        v.append("or raise the GPU wired limit (README):  "
                 "sudo sysctl iogpu.wired_limit_mb=<~90% of RAM in MB>")
        return v

    if sreal and (not sreal.get("ok") or _is_empty(sreal)):
        v.append("DIAGNOSIS: trivial prompts generate fine at all frame counts, but the "
                 "full courtside clip prompt produces nothing - prompt-specific.")
        v.append("Please share this doctor output; the prompt will be adjusted.")
        return v

    v.append("All steps produced text - the generation stack looks healthy.")
    v.append("If the full pipeline still fails, re-run it now and share clip_000.raw.txt.")
    return v


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="courtside-doctor", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=config.DEFAULT_MODEL_KEY,
                   help=f"registry key or HF repo (default: {config.DEFAULT_MODEL_KEY})")
    p.add_argument("--video", type=Path, default=None,
                   help="use real frames from this video (default: synthetic test frame)")
    p.add_argument("--max-side", type=int, default=config.DEFAULT_MAX_SIDE)
    p.add_argument("--max-tokens", type=int, default=80)
    args = p.parse_args(argv)

    require_tools()
    env = _versions()
    _p("=== courtside-doctor ===")
    for k, val in env.items():
        _p(f"  {k}: {val}")
    if env.get("mlx_vlm") == "NOT INSTALLED":
        _p("\nmlx-vlm is not installed - install with: pip install -e '.[local]' (Apple Silicon)")
        return 2

    repo = config.resolve_model(args.model)
    _p(f"\nloading {repo} ...")
    try:
        from mlx_vlm import load
        t0 = time.perf_counter()
        model, processor = load(repo)
        _p(f"loaded in {time.perf_counter() - t0:.1f}s")
    except Exception as e:
        _p(f"LOAD FAILED: {type(e).__name__}: {e}")
        return 2

    steps: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        video = args.video.resolve() if args.video else None

        for key, n in (("img1", 1), ("img8", 8), ("img32", 32)):
            frames = _make_frames(video, n, args.max_side, wd)
            steps[key] = _gen_step(model, processor, TRIVIAL_PROMPT, frames, args.max_tokens)
            _show(f"step {key}: {n} image(s), trivial prompt", steps[key])
            if not steps[key].get("ok") and key == "img1":
                break  # no point escalating if the basics raise

        # the real courtside prompt on 8 frames (only if basics generate)
        if steps.get("img1", {}).get("ok") and not _is_empty(steps["img1"]):
            frames = _make_frames(video, 8, args.max_side, wd)
            real_prompt = SYSTEM + "\n\n" + build_clip_prompt(
                n_frames=len(frames), start_s=0.0, end_s=20.0, schema=clip_json_schema(),
                timestamps=[i / 0.4 for i in range(len(frames))],
                flag_vocab=", ".join(sorted(CANONICAL_FLAG_CODES)),
            )
            steps["real"] = _gen_step(model, processor, real_prompt, frames, max(args.max_tokens, 400))
            _show("step real: 8 image(s), full courtside clip prompt", steps["real"])

    _p("\n=== verdict ===")
    for line in _verdict(env, steps):
        _p("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
